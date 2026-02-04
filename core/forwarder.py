import asyncio
import os
import re
from datetime import datetime, timezone
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

    负责：
    1. 监控源频道更新
    2. 过滤消息
    3. 分发给各平台 Sender
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

        # 任务锁，防止重入 (Key: ChannelName)
        self._channel_locks = {}
        # 上次检查时间 (Key: ChannelName)
        self._channel_last_check = {}
        # 全局发送锁，确保所有频道的消息按顺序发送，避免交错
        self._global_send_lock = asyncio.Lock()

    def _get_channel_lock(self, channel_name):
        if channel_name not in self._channel_locks:
            self._channel_locks[channel_name] = asyncio.Lock()
        return self._channel_locks[channel_name]

    async def check_updates(self):
        """
        检查所有配置的频道更新并加入待发送队列
        """
        if not self.client_wrapper.is_connected():
            return

        channels_config = self.config.get("source_channels", [])
        logger.debug(f"[Capture] 开始检查 Telegram 频道更新 (共 {len(channels_config)} 个频道)...")

        async def fetch_one(cfg):
            try:
                channel_name = ""
                start_date = None
                default_interval = 0
                interval = default_interval
                msg_limit = 20

                if isinstance(cfg, dict):
                    channel_name = cfg.get("channel_username", "")
                    if not channel_name: return []
                    s_time = cfg.get("start_time", "")
                    if s_time:
                        try:
                            start_date = datetime.strptime(s_time, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except: pass
                    interval = cfg.get("check_interval", default_interval)
                    msg_limit = cfg.get("msg_limit", 20)
                elif isinstance(cfg, str):
                    parts = [p.strip() for p in cfg.split("|")]
                    if not parts: return []
                    channel_name = parts[0]
                else: return []

                if not channel_name: return []

                now = datetime.now().timestamp()
                last_check = self._channel_last_check.get(channel_name, 0)
                if now - last_check < interval:
                    return []

                lock = self._get_channel_lock(channel_name)
                if lock.locked(): 
                    logger.debug(f"[Capture] 频道 {channel_name} 正在抓取中，跳过本次。")
                    return []

                async with lock:
                    self._channel_last_check[channel_name] = now
                    logger.debug(f"[Capture] 正在拉取频道 {channel_name} 的消息...")
                    messages = await self._fetch_channel_messages(channel_name, start_date, msg_limit)
                    
                    if messages:
                        # 先加入队列，再更新 last_id
                        for m in messages:
                            self.storage.add_to_pending_queue(channel_name, m.id, m.date.timestamp(), m.grouped_id)
                        
                        max_id = max(m.id for m in messages)
                        self.storage.update_last_id(channel_name, max_id)
                        
                        logger.debug(f"[Capture] 频道 {channel_name} 的 {len(messages)} 条新消息已成功存入本地待发送队列，并更新 last_id 为 {max_id}。")
                    else:
                        logger.debug(f"[Capture] 频道 {channel_name} 检查完成，无新消息。")
                    return messages
            except Exception as e:
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
        all_pending = self.storage.get_all_pending()
        queue_size = len(all_pending) if all_pending else 0
        
        if not all_pending:
            logger.debug("[Send] 正在检测待发送队列... 队列为空，无需处理。")
            return

        # 统计各频道积压情况
        stats = {}
        for item in all_pending:
            c = item["channel"]
            stats[c] = stats.get(c, 0) + 1
        
        stats_str = ", ".join([f"{c}: {n}条" for c, n in stats.items()])
        logger.debug(f"[Send] 正在检测待发送队列... 总计: {queue_size} 条消息 | 积压详情: {stats_str}")

        batch_limit = self.config.get("batch_size_limit", 3)
        retention = self.config.get("retention_period", 86400)
        now_ts = datetime.now().timestamp()

        valid_pending = []
        expired_count = 0
        for item in all_pending:
            if now_ts - item["time"] <= retention:
                valid_pending.append(item)
            else:
                expired_count += 1
        
        if expired_count > 0:
            self.storage.cleanup_expired_pending(retention)
            # 重新获取一下，因为清理了过期消息
            all_pending = self.storage.get_all_pending()
            valid_pending = all_pending # 此时 all_pending 已经是清理过的了

        if not valid_pending:
            logger.debug("[Send] 待发送队列为空。")
            return

        valid_pending.sort(key=lambda x: x["time"], reverse=True)
        
        logger.debug(f"[Send] 准备处理 {min(len(valid_pending), batch_limit)} 个逻辑批次 (batch_limit={batch_limit})")

        to_send_meta = []
        processed_ids = set()
        logical_count = 0 
        
        for item in valid_pending:
            if logical_count >= batch_limit:
                break
            
            if item["id"] in processed_ids:
                continue
            
            if item.get("grouped_id"):
                gid = item["grouped_id"]
                channel = item["channel"]
                album_items = [i for i in valid_pending if i.get("grouped_id") == gid and i["channel"] == channel]
                for a_item in album_items:
                    if a_item["id"] not in processed_ids:
                        to_send_meta.append(a_item)
                        processed_ids.add(a_item["id"])
                logical_count += 1 
            else:
                to_send_meta.append(item)
                processed_ids.add(item["id"])
                logical_count += 1 

        if not to_send_meta:
            return

        channel_to_ids = {}
        id_to_meta = {} # 建立 ID 到元数据的映射，方便后续根据 grouped_id 过滤
        for item in to_send_meta:
            c = item["channel"]
            mid = item["id"]
            if c not in channel_to_ids: channel_to_ids[c] = []
            channel_to_ids[c].append(mid)
            id_to_meta[mid] = item

        messages_to_process = []
        filter_keywords = self.config.get("filter_keywords", [])
        filter_regex = self.config.get("filter_regex", "")
        filter_hashtags = self.config.get("filter_hashtags", [])

        # 第一阶段：初步抓取并识别需要过滤的消息/相册
        raw_fetched_messages = []
        skipped_grouped_ids = set() # (channel, grouped_id)
        individually_skipped_ids = set()

        for channel, ids in channel_to_ids.items():
            try:
                msgs = await self.client.get_messages(channel, ids=ids)
                for m in msgs:
                    if not m: continue
                    raw_fetched_messages.append((channel, m))
                    
                    text_content = m.text or ""
                    # 检查是否有按钮文字 (广告常用)
                    button_text = ""
                    if m.reply_markup and hasattr(m.reply_markup, 'rows'):
                        btn_parts = []
                        for row in m.reply_markup.rows:
                            for btn in row.buttons:
                                if hasattr(btn, 'text'): btn_parts.append(btn.text)
                        button_text = " ".join(btn_parts)
                    
                    full_check_text = f"{text_content} {button_text}"

                    should_skip = False
                    
                    # 关键词/正则/Hashtag 过滤 (检查正文 + 按钮)
                    check_text_lower = full_check_text.lower()
                    
                    def is_keyword_matched(pattern_str, text):
                        pattern_str = pattern_str.lower()
                        if not pattern_str: return False
                        if pattern_str.isascii():
                            # 使用自定义边界：前后不能是英文字母或数字
                            regex_pattern = rf"(?<![a-zA-Z0-9]){re.escape(pattern_str)}(?![a-zA-Z0-9])"
                            return bool(re.search(regex_pattern, text, re.IGNORECASE))
                        # 对于包含非 ASCII（如中文）的关键词，维持原有的子串匹配
                        return pattern_str in text

                    if filter_hashtags:
                        for tag in filter_hashtags:
                            if is_keyword_matched(tag, check_text_lower):
                                logger.info(f"[Filter] 消息 {m.id} 命中 Hashtag '{tag}'")
                                should_skip = True; break
                    
                    if not should_skip and filter_keywords:
                        for kw in filter_keywords:
                            if is_keyword_matched(kw, check_text_lower):
                                logger.info(f"[Filter] 消息 {m.id} 命中关键词 '{kw}'")
                                should_skip = True; break
                    
                    if not should_skip and filter_regex:
                        if re.search(filter_regex, full_check_text, re.IGNORECASE | re.DOTALL):
                            logger.info(f"[Filter] 消息 {m.id} 命中正则匹配")
                            should_skip = True
                    
                    if should_skip:
                        meta = id_to_meta.get(m.id)
                        if meta and meta.get("grouped_id"):
                            skipped_grouped_ids.add((channel, meta["grouped_id"]))
                        individually_skipped_ids.add(m.id)
            except Exception as e:
                logger.error(f"[Send] 拉取消息失败 {channel}: {e}")

        # 第二阶段：应用过滤（包括相册联动过滤）
        for channel, m in raw_fetched_messages:
            meta = id_to_meta.get(m.id)
            is_in_skipped_album = False
            if meta and meta.get("grouped_id"):
                if (channel, meta["grouped_id"]) in skipped_grouped_ids:
                    is_in_skipped_album = True
            
            if m.id in individually_skipped_ids or is_in_skipped_album:
                if is_in_skipped_album and m.id not in individually_skipped_ids:
                    logger.info(f"[Filter] 消息 {m.id} 因所属相册中其他消息命中过滤规则而被同步跳过。")
                continue
            
            messages_to_process.append((channel, m))

        if not messages_to_process:
            processed_ids = [item["id"] for item in to_send_meta]
            # 建立频道到 ID 的反向索引，用于精确移除
            chan_to_ids_processed = {}
            for item in to_send_meta:
                c = item["channel"]
                if c not in chan_to_ids_processed: chan_to_ids_processed[c] = []
                chan_to_ids_processed[c].append(item["id"])
            
            for channel, ids in chan_to_ids_processed.items():
                self.storage.remove_ids_from_pending(channel, ids)

            logger.info(f"[Send] 本批次所有消息 ({len(processed_ids)} 条) 均被过滤或获取失败，已从队列移除。")
            return

        final_batches = []
        msg_map = {m.id: (c, m) for c, m in messages_to_process}
        processed_ids_in_send = set()
        
        for item in to_send_meta:
            mid = item["id"]
            if mid in msg_map and mid not in processed_ids_in_send:
                channel = item["channel"]
                if item.get("grouped_id"):
                    gid = item["grouped_id"]
                    album_items = [i for i in to_send_meta if i.get("grouped_id") == gid and i["channel"] == channel]
                    album_msgs = []
                    for ai in album_items:
                        if ai["id"] in msg_map:
                            album_msgs.append(msg_map[ai["id"]][1])
                            processed_ids_in_send.add(ai["id"])
                    
                    album_msgs.sort(key=lambda m: m.date)
                    final_batches.append((album_msgs, channel))
                else:
                    final_batches.append(([msg_map[mid][1]], channel))
                    processed_ids_in_send.add(mid)

        actual_sent_count = 0
        try:
            if final_batches:
                await self._send_sorted_messages_in_batches(final_batches)
                for msgs, _ in final_batches:
                    actual_sent_count += len(msgs)
        except Exception as e:
            logger.error(f"[Send] 转发过程出现错误: {e}")
        finally:
            # 建立频道到 ID 的反向索引，用于精确移除
            chan_to_ids_processed = {}
            for item in to_send_meta:
                c = item["channel"]
                if c not in chan_to_ids_processed: chan_to_ids_processed[c] = []
                chan_to_ids_processed[c].append(item["id"])
            
            for channel, ids in chan_to_ids_processed.items():
                self.storage.remove_ids_from_pending(channel, ids)
            
            if to_send_meta:
                processed_count = len(to_send_meta)
                skipped_count = processed_count - actual_sent_count
                msg = f"[Send] 批次处理完成。本批次共处理 {processed_count} 条："
                if actual_sent_count > 0:
                    msg += f" 成功发送 {actual_sent_count} 条；"
                if skipped_count > 0:
                    msg += f" 过滤/跳过 {skipped_count} 条；"
                
                # 获取最新队列大小
                new_all_pending = self.storage.get_all_pending()
                msg += f"队列剩余: {len(new_all_pending)} 条。"
                logger.info(msg)

    async def _send_sorted_messages_in_batches(self, batches_with_channel: List[tuple]):
        """发送排好序的消息批次"""
        async with self._global_send_lock:
            for msgs, src_channel in batches_with_channel:
                # 1. 转发到 QQ
                await self.qq_sender.send([msgs], src_channel)
                
                # 2. 转发到 Telegram
                await self.tg_sender.send([msgs], src_channel)

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
            if last_id == 0:
                if start_date:
                    logger.info(f"[Fetch] {channel_name} 正在从 {start_date} 开始冷启动...")
                    pass
                else:
                    msgs = await self.client.get_messages(channel_name, limit=1)
                    if msgs:
                        self.storage.update_last_id(channel_name, msgs[0].id)
                        logger.info(f"[Fetch] {channel_name} 首次运行/冷启动，已同步当前最新 ID: {msgs[0].id}，下次抓取将从此开始。")
                    return []

            new_messages = []
            params = {"entity": channel_name, "reverse": True, "limit": msg_limit}

            if last_id > 0:
                params["min_id"] = last_id
                logger.debug(f"[Fetch] {channel_name} 发起请求: min_id={last_id}, limit={msg_limit}")
            elif start_date:
                params["offset_date"] = start_date
            else:
                params["limit"] = 5

            async for message in self.client.iter_messages(**params):
                if not message.id:
                    continue
                new_messages.append(message)

            if new_messages:
                logger.debug(
                    f"[Fetch] {channel_name}: 抓取成功！获取到 {len(new_messages)} 条新消息 (范围: {min(m.id for m in new_messages)} -> {max(m.id for m in new_messages)})"
                )
            else:
                # 即使没有新消息，我们也尝试获取一下频道当前的最新 ID 用于日志显示
                latest_msgs = await self.client.get_messages(channel_name, limit=1)
                latest_id_on_tg = latest_msgs[0].id if latest_msgs else "未知"
                logger.debug(f"[Fetch] {channel_name}: 没有新消息。 (当前 TG 最新 ID: {latest_id_on_tg})")

            return new_messages

        except Exception as e:
            logger.error(f"[Fetch] 访问 {channel_name} 失败: {e}")
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
