import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from telethon.tl.types import Message, PeerUser

from astrbot.api import logger, AstrBotConfig, star
from ..common.storage import Storage
from .client import TelegramClientWrapper
from .downloader import MediaDownloader
from .uploader import FileUploader
from .senders.telegram import TelegramSender
from .senders.qq import QQSender
from .filters.message_filter import MessageFilter
from .mergers import MessageMerger


class Forwarder:
    """
    消息转发器核心类 (Monitor + Dispatcher)
    """

    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig,
        storage: Storage,
        client_wrapper: TelegramClientWrapper,
        plugin_data_dir: str,
    ):
        self.context = context
        self.config = config
        self.storage = storage
        self.client_wrapper = client_wrapper
        self.client = client_wrapper.client
        self.plugin_data_dir = plugin_data_dir
        self.proxy_url = config.get("proxy")
        self._stopping = False # 新增停止标志

        # 初始化组件
        self.downloader = MediaDownloader(self.client, plugin_data_dir)
        self.uploader = FileUploader(self.proxy_url)

        # 初始化发送器
        self.tg_sender = TelegramSender(self.client, config)
        self.qq_sender = QQSender(self.context, config, self.downloader, self.uploader)

        # 初始化过滤器和合并引擎
        self.message_filter = MessageFilter(config)
        self.message_merger = MessageMerger(config)

        # 启动时清理孤儿文件
        self._cleanup_orphaned_files()

        # 启动时重置不在配置中的频道的 last_post_id
        source_channels = config.get("source_channels", [])
        active_channels = [c.get("channel_username") for c in source_channels if c.get("channel_username")]
        logger.debug(f"[Capture] 当前活跃监控频道列表: {active_channels}")
        self.storage.reset_inactive_channels(active_channels)

        # 任务锁，防止重入 (Key: ChannelName)
        self._channel_locks = {}
        # 上次检查时间 (Key: ChannelName)
        self._channel_last_check = {}
        # 全局发送锁，确保所有频道的消息按顺序发送，避免交错
        self._global_send_lock = asyncio.Lock()

        # 缓存频道标题 (Key: ChannelUsername, Value: Title)
        self._channel_titles_cache = {}

    def _get_channel_lock(self, channel_name: str) -> asyncio.Lock:
        if channel_name not in self._channel_locks:
            self._channel_locks[channel_name] = asyncio.Lock()
        return self._channel_locks[channel_name]

    async def _get_display_name(self, channel_name: str) -> str:
        """获取频道显示名称"""
        forward_cfg = self.config.get("forward_config", {})
        use_title = forward_cfg.get("use_channel_title", True)

        if not use_title:
            return f"@{channel_name}"

        # 尝试从缓存获取
        if channel_name in self._channel_titles_cache:
            return self._channel_titles_cache[channel_name]

        # 尝试从 Telegram 获取
        try:
            entity = await self.client.get_entity(channel_name)
            title = getattr(entity, 'title', channel_name)
            self._channel_titles_cache[channel_name] = title
            return title
        except Exception as e:
            logger.warning(f"[Capture] 无法获取频道 {channel_name} 的标题: {e}")
            return f"@{channel_name}"

    def _get_effective_config(self, channel_name: str):
        """
        获取有效配置 (双重过滤原则: 全局与频道配置均需符合)
        """
        # 1. 获取全局配置
        global_cfg = self.config.get("forward_config", {})
        
        # 2. 获取该频道的特定配置
        channels_config = self.config.get("source_channels", [])
        channel_cfg = {}
        for cfg in channels_config:
            if cfg.get("channel_username") == channel_name:
                channel_cfg = cfg
                break
        
        # 3. 核心过滤项交集逻辑 ( Strictest Policy )
        
        # 3.1 转发类型 (交集)
        g_types = set(global_cfg.get("forward_types", ["文字", "图片", "视频", "音频", "文件"]))
        c_types = set(channel_cfg.get("forward_types", ["文字", "图片", "视频", "音频", "文件"]))
        forward_types = list(g_types.intersection(c_types))
        
        # 3.2 文件大小限制 (取非零最小值)
        g_max = global_cfg.get("max_file_size", 0)
        c_max = channel_cfg.get("max_file_size", 0)
        if g_max > 0 and c_max > 0:
            max_file_size = min(g_max, c_max)
        else:
            max_file_size = g_max or c_max # 只要有一个不为0就取那个，都为0则不限制
            
        # 3.3 关键词与正则 (并集过滤：命中任何一个都过滤)
        filter_keywords = list(set(global_cfg.get("filter_keywords", []) + channel_cfg.get("filter_keywords", [])))
        
        # 3.4 发送间隔与检测间隔
        check_interval = channel_cfg.get("check_interval") or global_cfg.get("check_interval", 60)
        send_interval = global_cfg.get("send_interval", 60)

        # 3.5 查重开关
        enable_deduplication = global_cfg.get("enable_deduplication", True)

        # 3.6 优先级校验 (小于 1 视作 0)
        priority = channel_cfg.get("priority", 0)
        if priority < 1:
            priority = 0

        return {
            "forward_types": forward_types,
            "max_file_size": max_file_size,
            "filter_keywords": filter_keywords,
            "check_interval": check_interval,
            "send_interval": send_interval,
            "enable_deduplication": enable_deduplication,
            "priority": priority,
            "exclude_text_on_media": channel_cfg.get("exclude_text_on_media", "继承全局") == "开启" or 
                                    (channel_cfg.get("exclude_text_on_media", "继承全局") == "继承全局" and global_cfg.get("exclude_text_on_media", False)),
            "start_time": channel_cfg.get("start_time", ""),
            "msg_limit": channel_cfg.get("msg_limit", 20)
        }

    def _is_curfew(self) -> bool:
        """检查当前是否处于宵禁时间"""
        forward_cfg = self.config.get("forward_config", {})
        curfew_time = forward_cfg.get("curfew_time", "").strip()
        if not curfew_time:
            return False

        try:
            # 格式校验 11:11-14:12
            if "-" not in curfew_time:
                return False
            
            start_str, end_str = curfew_time.split("-")
            start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
            end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
            now_time = datetime.now().time()

            if start_time <= end_time:
                # 非跨天情况 (例: 11:11-14:12)
                return start_time <= now_time <= end_time
            else:
                # 跨天情况 (例: 23:00-07:00)
                return now_time >= start_time or now_time <= end_time
        except Exception as e:
            logger.error(f"[Forwarder] 宵禁时间格式解析错误: {curfew_time}. 错误: {e}")
            return False

    async def check_updates(self):
        """
        检查所有配置的频道更新并加入待发送队列
        """
        if self._stopping:
            return

        if not self.client_wrapper.is_connected():
            return

        if self._is_curfew():
            logger.debug("[Capture] 当前处于宵禁时间，跳过拉取任务。")
            return

        channels_config = self.config.get("source_channels", [])
        logger.debug(f"[Capture] 开始检查 Telegram 频道更新 (共 {len(channels_config)} 个频道)...")

        async def fetch_one(cfg):
            try:
                channel_name = cfg.get("channel_username", "")
                if not channel_name: return []
                
                effective_cfg = self._get_effective_config(channel_name)
                
                interval = effective_cfg["check_interval"]
                msg_limit = effective_cfg["msg_limit"]

                # 1. 优先检查抓取间隔，没到时间直接退出，避免无效开销
                now = datetime.now().timestamp()
                last_check = self._channel_last_check.get(channel_name, 0)
                if now - last_check < interval:
                    return []

                # 2. 到时间了，再获取该频道上次拉取的最后一条消息 ID
                channel_data = self.storage.get_channel_data(channel_name)
                last_id = channel_data.get("last_post_id", 0)

                start_date = None
                s_time = effective_cfg.get("start_time", "")
                # 只有在 last_id 为 0 (说明从未成功拉取过，需要冷启动) 时，才执行日期转换逻辑
                if last_id == 0 and s_time:
                    try:
                        dt_naive = datetime.strptime(s_time, "%Y-%m-%d")
                        # 设为北京时间 00:00:00 (UTC+8)
                        tz_beijing = timezone(timedelta(hours=8))
                        dt_beijing = dt_naive.replace(tzinfo=tz_beijing)
                        # 转换为 UTC
                        start_date = dt_beijing.astimezone(timezone.utc)
                        logger.debug(f"[Capture] 频道 {channel_name} 冷启动日期转换: 输入 {s_time} (北京时间) -> 转换为 UTC: {start_date}")
                    except Exception as e:
                        logger.warning(f"[Capture] 频道 {channel_name} 冷启动日期格式错误 '{s_time}': {e}")
                        pass

                lock = self._get_channel_lock(channel_name)
                if lock.locked(): 
                    logger.debug(f"[Capture] 频道 {channel_name} 正在抓取中，跳过本次。")
                    return []

                async with lock:
                    self._channel_last_check[channel_name] = now
                    logger.debug(f"[Capture] 正在拉取: {channel_name}")
                    messages = await self._fetch_channel_messages(channel_name, start_date, msg_limit)
                    
                    if messages:
                        # 先加入队列，再更新 last_id
                        for m in messages:
                            self.storage.add_to_pending_queue(
                                channel_name, 
                                m.id, 
                                m.date.timestamp(), 
                                m.grouped_id,
                                is_cold_start=(last_id == 0 and start_date is not None)
                            )
                        
                        max_id = max(m.id for m in messages)
                        self.storage.update_last_id(channel_name, max_id)
                        
                        logger.info(f"[Capture] 频道 {channel_name} 成功拉取 {len(messages)} 条消息 (ID: {max_id})")
                    else:
                        logger.debug(f"[Capture] 频道 {channel_name} 无新消息。")
                    return messages
            except Exception as e:
                error_msg = str(e)
                if "database disk image is malformed" in error_msg:
                    logger.error(f"[Capture] Telethon 数据库文件损坏 (malformed)。可尝试重载插件以恢复...")
                    session_path = os.path.join(self.plugin_data_dir, "user_session")
                    from .client import TelegramClientWrapper
                    TelegramClientWrapper.clear_cache(session_path)
                logger.error(f"[Capture] 检查频道 {cfg} 时出现未捕获异常: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return []
            finally:
                if 'channel_name' in locals() and channel_name:
                    logger.debug(f"[Capture] 频道 {channel_name} 检查任务结束。")

        tasks = [fetch_one(cfg) for cfg in channels_config]
        if tasks:
            await asyncio.gather(*tasks)

    async def send_pending_messages(self):
        """
        从待发送队列中提取消息并执行转发
        """
        if self._stopping:
            return

        if self._is_curfew():
            logger.debug("[Send] 当前处于宵禁时间，跳过转发任务。")
            return

        all_pending = self.storage.get_all_pending()
        queue_size = len(all_pending) if all_pending else 0
        
        if not all_pending:
            logger.debug("[Send] 正在检测待发送队列... 队列为空，无需处理。")
            return

        # 获取全局配置用于提取公共参数
        global_cfg = self.config.get("forward_config", {})
        
        batch_limit = global_cfg.get("batch_size_limit", 3)
        retention = global_cfg.get("retention_period", 86400)
        now_ts = datetime.now().timestamp()

        # 统计各频道积压情况
        stats = {}
        for item in all_pending:
            c = item["channel"]
            stats[c] = stats.get(c, 0) + 1
        
        stats_str = ", ".join([f"{c}({n}条)" for c, n in stats.items()])
        logger.debug(f"[Send] 队列状态: 总计 {queue_size} 条 | 详情: {stats_str}")

        valid_pending = []
        expired_count = 0
        for item in all_pending:
            # 冷启动消息不检测过期时间
            if item.get("is_cold_start", False) or (now_ts - item["time"] <= retention):
                valid_pending.append(item)
            else:
                expired_count += 1
        
        if expired_count > 0:
            self.storage.cleanup_expired_pending(retention)
            all_pending = self.storage.get_all_pending()
            valid_pending = all_pending

        if not valid_pending:
            return

        # 优先级排序逻辑
        source_channels = self.config.get("source_channels", [])
        channel_priorities = {
            cfg.get("channel_username"): self._get_effective_config(cfg.get("channel_username"))["priority"] 
            for cfg in source_channels if cfg.get("channel_username")
        }
        
        # 优先级排序逻辑
        source_channels = self.config.get("source_channels", [])
        channel_priorities = {
            cfg.get("channel_username"): self._get_effective_config(cfg.get("channel_username"))["priority"] 
            for cfg in source_channels if cfg.get("channel_username")
        }
        
        # 排序规则：
        # 1. 优先级 (priority) 从大到小 (reverse=True)
        # 2. 消息类型标识：如果是冷启动产生的补旧消息，按时间正序（旧->新）；如果是日常新消息，按时间倒序（新->旧）
        # 3. 实时消息优先于补旧消息
        def sorting_key(x):
            prio = channel_priorities.get(x["channel"], 0)
            is_cold = x.get("is_cold_start", False)
            # 元组排序维度：
            # (优先级 DESC, 是否冷启动 ASC (False先排), 时间)
            # 如果是实时消息 (is_cold=False): 时间降序 (-time)
            # 如果是冷启动消息 (is_cold=True): 时间升序 (time)
            return (-prio, is_cold, -x["time"] if not is_cold else x["time"])

        valid_pending.sort(key=sorting_key)
        
        logger.debug(f"[Send] 开始处理待发送队列 (批次上限: {batch_limit})")

        final_batches = []
        all_processed_meta = []
        logical_sent_count = 0
        processed_ids = set()
        pending_idx = 0

        while logical_sent_count < batch_limit and pending_idx < len(valid_pending):
            # 1. 提取下一组元数据进行尝试
            current_try_meta = []
            current_try_logical_units = 0
            needed_units = batch_limit - logical_sent_count
            
            # 记录本轮尝试提取的逻辑单元对应的 ID，用于后续分组
            current_try_logical_map = {} # {logical_id: [meta_items]}

            while current_try_logical_units < needed_units and pending_idx < len(valid_pending):
                item = valid_pending[pending_idx]
                pending_idx += 1
                
                if item["id"] in processed_ids:
                    continue
                
                logical_id = item.get("grouped_id") or f"single_{item['id']}"
                if item.get("grouped_id"):
                    gid = item["grouped_id"]
                    channel = item["channel"]
                    album_items = [i for i in valid_pending if i.get("grouped_id") == gid and i["channel"] == channel]
                    
                    unit_items = []
                    for a_item in album_items:
                        if a_item["id"] not in processed_ids:
                            unit_items.append(a_item)
                            processed_ids.add(a_item["id"])
                    
                    if unit_items:
                        current_try_meta.extend(unit_items)
                        current_try_logical_map[logical_id] = unit_items
                        current_try_logical_units += 1
                else:
                    current_try_meta.append(item)
                    current_try_logical_map[logical_id] = [item]
                    processed_ids.add(item["id"])
                    current_try_logical_units += 1

            if not current_try_meta:
                break
                
            all_processed_meta.extend(current_try_meta)

            # 2. 抓取与初步过滤
            channel_to_ids = {}
            id_to_meta = {item["id"]: item for item in current_try_meta}
            for item in current_try_meta:
                c = item["channel"]; mid = item["id"]
                if c not in channel_to_ids: channel_to_ids[c] = []
                channel_to_ids[c].append(mid)

            raw_fetched_messages = []
            skipped_grouped_ids = set() # (channel, grouped_id)
            individually_skipped_ids = set()

            def is_keyword_matched(pattern_str, text):
                pattern_str = pattern_str.lower()
                if not pattern_str: return False
                if pattern_str.isascii():
                    regex_pattern = rf"(?<![a-zA-Z0-9]){re.escape(pattern_str)}(?![a-zA-Z0-9])"
                    return bool(re.search(regex_pattern, text, re.IGNORECASE))
                return pattern_str in text

            for channel, ids in channel_to_ids.items():
                try:
                    effective_cfg = self._get_effective_config(channel)
                    msgs = await self.client.get_messages(channel, ids=ids)
                    for m in msgs:
                        if not m: continue
                        raw_fetched_messages.append((channel, m))
                        
                        # 类型过滤
                        forward_types = effective_cfg["forward_types"]
                        max_file_size = effective_cfg["max_file_size"]
                        msg_type = "文字"
                        if m.photo: msg_type = "图片"
                        elif m.video: msg_type = "视频"
                        elif m.voice or m.audio: msg_type = "音频"
                        elif m.document: msg_type = "文件"
                        
                        if msg_type not in forward_types:
                            logger.info(f"[Filter] 消息 {m.id} 类型 '{msg_type}' 不在允许列表中，跳过。")
                            individually_skipped_ids.add(m.id)
                            continue

                        # 检查文件大小
                        m._max_file_size = max_file_size
                        if not m.photo and max_file_size > 0:
                            file_size = 0
                            if hasattr(m, "media") and m.media:
                                if hasattr(m.media, "document") and hasattr(m.media.document, "size"):
                                    file_size = m.media.document.size
                                elif hasattr(m.file, "size"):
                                    file_size = m.file.size
                            if file_size > max_file_size * 1024 * 1024:
                                logger.info(f"[Filter] 消息 {m.id} 文件大小 ({file_size / 1024 / 1024:.2f} MB) 超过限制 ({max_file_size} MB)，跳过。")
                                individually_skipped_ids.add(m.id)
                                continue

                        # 关键词/正则过滤
                        text_content = m.text or ""
                        button_text = ""
                        if m.reply_markup and hasattr(m.reply_markup, 'rows'):
                            btn_parts = [btn.text for row in m.reply_markup.rows for btn in row.buttons if hasattr(btn, 'text')]
                            button_text = " ".join(btn_parts)
                        
                        full_check_text = f"{text_content} {button_text}"
                        should_skip = False
                        check_text_lower = full_check_text.lower()
                        
                        filter_keywords = effective_cfg["filter_keywords"]
                        if filter_keywords:
                            for kw in filter_keywords:
                                if is_keyword_matched(kw, check_text_lower):
                                    logger.info(f"[Filter] 消息 {m.id} 命中关键词 '{kw}'")
                                    should_skip = True; break
                        
                        patterns = []
                        if global_cfg.get("filter_regex"): patterns.append(global_cfg["filter_regex"])
                        current_channel_raw_cfg = next((c for c in self.config.get("source_channels", []) if c.get("channel_username") == channel), {})
                        if current_channel_raw_cfg.get("filter_regex"): patterns.append(current_channel_raw_cfg["filter_regex"])

                        for pattern in patterns:
                            if not should_skip and pattern:
                                if re.search(pattern, full_check_text, re.IGNORECASE | re.DOTALL):
                                    logger.info(f"[Filter] 消息 {m.id} 命中正则匹配: {pattern[:30]}...")
                                    should_skip = True; break
                        
                        if should_skip:
                            individually_skipped_ids.add(m.id)
                            meta = id_to_meta.get(m.id)
                            if meta and meta.get("grouped_id"):
                                skipped_grouped_ids.add((channel, meta["grouped_id"]))
                except Exception as e:
                    error_msg = str(e)
                    if "database disk image is malformed" in error_msg:
                        logger.error(f"[Send] Telethon 数据库文件损坏 (malformed)。可尝试重载插件以恢复...")
                        session_path = os.path.join(self.plugin_data_dir, "user_session")
                        from .client import TelegramClientWrapper
                        TelegramClientWrapper.clear_cache(session_path)
                    logger.error(f"[Send] 拉取消息失败 {channel}: {e}")

            # 3. 应用过滤并构建本轮有效的 batches
            msg_map = {m.id: (c, m) for c, m in raw_fetched_messages}
            
            for logical_id, unit_items in current_try_logical_map.items():
                channel = unit_items[0]["channel"]
                is_album = unit_items[0].get("grouped_id") is not None
                
                if is_album:
                    gid = unit_items[0]["grouped_id"]
                    if (channel, gid) in skipped_grouped_ids:
                        continue # 整个相册被跳过
                    
                    album_msgs = []
                    for ui in unit_items:
                        mid = ui["id"]
                        if mid in msg_map and mid not in individually_skipped_ids:
                            album_msgs.append(msg_map[mid][1])
                    
                    if album_msgs:
                        album_msgs.sort(key=lambda m: m.date)
                        final_batches.append((album_msgs, channel))
                        logical_sent_count += 1
                else:
                    mid = unit_items[0]["id"]
                    if mid in msg_map and mid not in individually_skipped_ids:
                        final_batches.append(([msg_map[mid][1]], channel))
                        logical_sent_count += 1

        if not final_batches:
            if all_processed_meta:
                chan_to_ids_processed = {}
                for item in all_processed_meta:
                    c = item["channel"]
                    if c not in chan_to_ids_processed: chan_to_ids_processed[c] = []
                    chan_to_ids_processed[c].append(item["id"])
                for channel, ids in chan_to_ids_processed.items():
                    self.storage.remove_ids_from_pending(channel, ids)
                logger.info(f"[Send] 本批次尝试的所有消息 ({len(all_processed_meta)} 条) 均被过滤或获取失败，已从队列移除。")
            return

        actual_sent_count = 0
        try:
            await self._send_sorted_messages_in_batches(final_batches)
            for msgs, _ in final_batches:
                actual_sent_count += len(msgs)
        except Exception as e:
            logger.error(f"[Send] 转发过程出现错误: {e}")
        finally:
            chan_to_ids_processed = {}
            for item in all_processed_meta:
                c = item["channel"]
                if c not in chan_to_ids_processed: chan_to_ids_processed[c] = []
                chan_to_ids_processed[c].append(item["id"])
            for channel, ids in chan_to_ids_processed.items():
                self.storage.remove_ids_from_pending(channel, ids)
            
            if all_processed_meta:
                processed_count = len(all_processed_meta)
                skipped_count = processed_count - actual_sent_count
                msg = f"[Send] 处理完成: 成功 {actual_sent_count}"
                if skipped_count > 0:
                    msg += f" | 跳过 {skipped_count}"
                new_all_pending = self.storage.get_all_pending()
                msg += f" | 剩余队列: {len(new_all_pending)}"
                logger.info(msg)


    async def _send_sorted_messages_in_batches(self, batches_with_channel: List[tuple]):
        """发送排好序的消息批次"""
        async with self._global_send_lock:
            for msgs, src_channel in batches_with_channel:
                effective_cfg = self._get_effective_config(src_channel)
                display_name = await self._get_display_name(src_channel)
                # 1. 转发到 QQ
                await self.qq_sender.send([msgs], src_channel, display_name, effective_cfg["exclude_text_on_media"])
                
                # 2. 转发到 Telegram
                await self.tg_sender.send([msgs], display_name, effective_cfg)

    def stop(self):
        """停止转发器工作"""
        self._stopping = True

    async def _fetch_channel_messages(
        self, channel_name: str, start_date: Optional[datetime], msg_limit: int = 20
    ) -> List[Message]:
        """
        从单个频道获取新消息
        """
        if not self.storage.get_channel_data(channel_name).get("last_post_id"):
            self.storage.update_last_id(channel_name, 0)

        last_id = self.storage.get_channel_data(channel_name)["last_post_id"]
        logger.debug(f"[Fetch] 频道: {channel_name} | 记录的最新 ID (last_id): {last_id}")

        try:
            effective_cfg = self._get_effective_config(channel_name)
            enable_dedup = effective_cfg.get("enable_deduplication", True)

            # 0. 获取频道实体并记录 ID (用于查重)
            entity = await self.client.get_input_entity(channel_name)
            if hasattr(entity, "channel_id"):
                self.storage.update_channel_id(channel_name, entity.channel_id)
            
            new_messages = []
            
            # 1. 如果没有上次拉取的 ID
            if last_id == 0:
                if start_date:
                    # 执行冷启动：从指定日期开始向后抓取
                    params = {
                        "entity": channel_name,
                        "reverse": True, 
                        "offset_date": start_date,
                        "limit": 1000 # 冷启动设置安全上限
                    }
                    logger.info(f"[Fetch] {channel_name}: 首次运行，执行冷启动，从 {start_date.strftime('%Y-%m-%d')} 开始拉取历史消息")
                else:
                    # 无冷启动设置：初始化 last_id 为最新消息 ID，不搬运旧消息
                    msgs = await self.client.get_messages(channel_name, limit=1)
                    if msgs:
                        self.storage.update_last_id(channel_name, msgs[0].id)
                        logger.info(f"[Fetch] {channel_name}: 首次运行且无冷启动设置，初始化 ID -> {msgs[0].id}")
                    return []
            else:
                # 2. 正常增量抓取
                params = {
                    "entity": channel_name,
                    "reverse": True,
                    "min_id": last_id,
                    "limit": msg_limit
                }
                logger.debug(f"[Fetch] {channel_name}: 增量拉取，ID > {last_id}")

            async for message in self.client.iter_messages(**params):
                if not message.id:
                    continue
                
                # --- 转发查重逻辑 ---
                if enable_dedup and message.fwd_from and message.fwd_from.from_id:
                    from telethon.tl.types import PeerChannel
                    if isinstance(message.fwd_from.from_id, PeerChannel):
                        src_channel_id = message.fwd_from.from_id.channel_id
                        orig_msg_id = message.fwd_from.channel_post
                        
                        # 查找该 ID 是否对应我们正在监控的某个频道
                        src_channel_name = self.storage.get_channel_name_by_id(src_channel_id)
                        if src_channel_name:
                            # 检查原消息是否已经处理过 (通过比较原频道的 last_post_id)
                            src_data = self.storage.get_channel_data(src_channel_name)
                            src_last_id = src_data.get("last_post_id", 0)
                            
                            if orig_msg_id <= src_last_id:
                                logger.debug(f"[Fetch] 频道 {channel_name} 的消息 {message.id} 是转发自监控频道 {src_channel_name} 的旧消息 (原 ID: {orig_msg_id} <= 已处理 ID: {src_last_id})，自动跳过。")
                                continue
                # ------------------

                new_messages.append(message)

            return new_messages

        except Exception as e:
            error_msg = str(e)
            if "database disk image is malformed" in error_msg:
                logger.error(f"[Fetch] Telethon 数据库文件损坏 (malformed)。建议重载插件。")
            logger.error(f"[Fetch] {channel_name}: 访问失败 - {e}")
            return []

    def _cleanup_orphaned_files(self):
        """
        启动时清理插件数据目录中的孤儿文件
        """
        if not os.path.exists(self.plugin_data_dir):
            return

        logger.debug(f"[Cleanup] 正在清理临时文件: {self.plugin_data_dir}")
        allowlist = [
            "data.json",
            "user_session.session",
            "user_session.session-journal",
            "user_session.session-shm",
            "user_session.session-wal",
        ]
        deleted_count = 0

        try:
            for filename in os.listdir(self.plugin_data_dir):
                if filename in allowlist:
                    continue

                file_path = os.path.join(self.plugin_data_dir, filename)

                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except Exception:
                        pass

            if deleted_count > 0:
                logger.debug(f"[Cleanup] 清理完成，移除了 {deleted_count} 个孤儿文件。")

        except Exception as e:
            logger.error(f"[Cleanup] 清理文件失败: {e}")
