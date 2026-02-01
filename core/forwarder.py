"""
消息转发核心模块

负责从 Telegram 频道获取消息并转发到目标平台（Telegram 频道或 QQ 群）。
主要功能：
- 定期检查频道更新
- 消息过滤（关键词、正则表达式）
- 冷启动支持（从指定日期开始）
- 媒体文件下载和处理
- 多平台消息发送
"""

import asyncio
import re
import os
import httpx
from datetime import datetime, timezone
from typing import Optional
from telethon.tl.types import Message, PeerUser

from astrbot.api import logger, AstrBotConfig
from ..common.text_tools import clean_telegram_text
from ..common.storage import Storage
from .client import TelegramClientWrapper
from .uploader import FileUploader

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB

class Forwarder:
    """
    消息转发器核心类

    负责从 Telegram 源频道获取消息，处理后转发到目标平台。
    支持 Telegram-to-Telegram 和 Telegram-to-QQ 两种转发模式。
    """
    def __init__(self, config: AstrBotConfig, storage: Storage, client_wrapper: TelegramClientWrapper, plugin_data_dir: str):
        """
        初始化转发器

        Args:
            config: AstrBot 配置对象，包含源频道、目标频道、过滤规则等
            storage: 数据持久化管理器，用于记录已处理的消息ID
            client_wrapper: Telegram 客户端封装
            plugin_data_dir: 插件数据目录，用于临时存储下载的媒体文件
        """
        self.config = config
        self.storage = storage
        self.client_wrapper = client_wrapper
        self.client = client_wrapper.client  # 快捷访问
        self.plugin_data_dir = plugin_data_dir
        self.proxy_url = config.get("proxy")  # Initialize proxy_url
        self.uploader = FileUploader(self.proxy_url)
        
        # Perform startup cleanup
        self._cleanup_orphaned_files()



    async def check_updates(self):
        """
        检查所有配置的频道更新

        执行流程：
        1. 检查客户端连接状态
        2. 遍历所有配置的源频道
        3. 解析频道配置（支持日期过滤）
        4. 调用 _process_channel 处理每个频道

        频道配置格式：
            - "channel_name" - 从最新消息开始
            - "channel_name|2024-01-01" - 从指定日期开始

        异常处理：
            - 单个频道处理失败不影响其他频道
            - 每个频道的错误会被记录日志
        """
        self.proxy_url = self.config.get("proxy")  # Get proxy from config
        
        # 检查连接状态
        if not self.client_wrapper.is_connected():
            return

        # 获取源频道配置列表
        channels_config = self.config.get("source_channels", [])

        # ========== 并行处理所有频道 ==========
        
        async def process_one(cfg):
            try:
                channel_name = cfg
                start_date = None

                # 解析频道配置（支持日期过滤）
                # 格式：channel_name|YYYY-MM-DD
                if "|" in cfg:
                    channel_name, date_str = cfg.split("|", 1)
                    channel_name = channel_name.strip()
                    try:
                         # 将字符串转换为时区感知的 datetime 对象
                         start_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except:
                        pass
                else:
                    channel_name = cfg.strip()

                # 处理该频道
                # logger.debug(f"Start checking {channel_name}...")
                await self._process_channel(channel_name, start_date)
            except Exception as e:
                # 记录错误但继续处理其他频道
                logger.error(f"Error checking {cfg}: {e}")

        # 创建所有任务
        tasks = [process_one(cfg) for cfg in channels_config]
        
        # 并发执行 (Gather all tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def _process_channel(self, channel_name: str, start_date: Optional[datetime]):
        """
        处理单个频道的消息更新

        Args:
            channel_name: 频道名称或ID
            start_date: 可选的开始日期，用于冷启动时从指定日期获取消息

        执行流程：
        1. 初始化或获取频道最后处理的消息ID
        2. 处理冷启动逻辑（首次运行）
        3. 获取新消息
        4. 应用过滤规则（关键词、正则）
        5. 逐条转发并更新持久化状态

        冷启动逻辑：
            - 有日期：从指定日期开始获取
            - 无日期：只获取最新消息ID，不处理历史
        """
        # ========== 初始化频道状态 ==========
        if not self.storage.get_channel_data(channel_name).get("last_post_id"):
             self.storage.update_last_id(channel_name, 0)  # 确保初始化

        last_id = self.storage.get_channel_data(channel_name)["last_post_id"]

        try:
            # ========== 冷启动处理 ==========
            if last_id == 0:
                if start_date:
                    # 有日期配置：从指定日期开始获取历史消息
                    logger.info(f"Cold start for {channel_name} with date {start_date}")
                    pass  # 逻辑在迭代参数中处理
                else:
                    # 无日期配置：跳过历史，只记录最新消息ID
                    # 这样可以避免首次启动时转发大量历史消息
                     msgs = await self.client.get_messages(channel_name, limit=1)
                     if msgs:
                         self.storage.update_last_id(channel_name, msgs[0].id)
                         logger.info(f"Initialized {channel_name} at ID {msgs[0].id}")
                     return

            # ========== 获取新消息 ==========
            new_messages = []

            # 构建消息迭代参数
            params = {"entity": channel_name, "reverse": True, "limit": 20}

            if last_id > 0:
                 # 常规运行：获取 ID 大于 last_id 的新消息
                 params["min_id"] = last_id
            elif start_date:
                 # 冷启动有日期：从该日期开始获取
                 params["offset_date"] = start_date
            else:
                 # 冷启动无日期：获取少量最新消息
                 params["limit"] = 5

            # 使用迭代器获取消息（支持分页，内存友好）
            async for message in self.client.iter_messages(**params):
                if not message.id: continue
                new_messages.append(message)

            # 没有新消息则返回
            if not new_messages:
                return

            # ========== 获取过滤配置 ==========
            filter_keywords = self.config.get("filter_keywords", [])
            filter_regex = self.config.get("filter_regex", "")

            final_last_id = last_id

            # ========== 处理每条消息 ==========
            
            # 缓冲待发送的消息组
            pending_batch = []
            
            async def process_batch(batch):
                if not batch: return
                # 过滤 batch 中的消息
                batch_to_send = []
                for msg in batch:
                     try:
                        # ----- 反垃圾 / 频道过滤 -----
                        is_user_msg = isinstance(msg.from_id, PeerUser) if msg.from_id else False
                        if not msg.post and is_user_msg: continue

                        text_content = msg.text or ""

                        # ----- 关键词过滤 -----
                        should_skip = False
                        filter_hashtags = self.config.get("filter_hashtags", [])
                        
                        # Hashtag 过滤 (精确匹配带 # 的标签)
                        if filter_hashtags:
                            for tag in filter_hashtags:
                                if tag in text_content:
                                    logger.info(f"Filtered {msg.id}: Hashtag {tag}")
                                    should_skip = True
                                    break

                        if not should_skip and filter_keywords:
                            for kw in filter_keywords:
                                if kw in text_content:
                                    logger.info(f"Filtered {msg.id}: Keyword {kw}")
                                    should_skip = True
                                    break
                        
                        # ----- 正则表达式过滤 -----
                        if not should_skip and filter_regex:
                            if re.search(filter_regex, text_content, re.IGNORECASE | re.DOTALL):
                                logger.info(f"Filtered {msg.id}: Regex")
                                should_skip = True
                        
                        if not should_skip:
                            batch_to_send.append(msg)
                     except Exception as e:
                         logger.error(f"Error filtering msg {msg.id}: {e}")

                if batch_to_send:
                    try:
                        await self._forward_message(channel_name, batch_to_send)
                        
                        # 更新 last_id 为 batch 中最大的 ID
                        max_id = max(m.id for m in batch)
                        self.storage.update_last_id(channel_name, max_id)
                        
                        # 速率限制
                        delay = self.config.get("forward_delay", 0)
                        if delay > 0: await asyncio.sleep(delay)
                    except Exception as e:
                         logger.error(f"Failed to forward batch (first id {batch[0].id}): {e}")


            for msg in new_messages:
                try:
                    # 如果当前消息属于相册 (有 grouped_id)
                    if msg.grouped_id:
                        # 如果缓冲区有消息，且属于不同组 -> 先处理缓冲区
                        if pending_batch and pending_batch[0].grouped_id != msg.grouped_id:
                             await process_batch(pending_batch)
                             pending_batch = [] # 清空
                        
                        # 加入当前消息到缓冲区
                        pending_batch.append(msg)
                    
                    else:
                        # 当前消息是独立的
                        # 1. 先处理之前的缓冲区 (如果有)
                        if pending_batch:
                             await process_batch(pending_batch)
                             pending_batch = []

                        # 2. 直接处理当前消息
                        await process_batch([msg])

                except Exception as e:
                    logger.error(f"Error in msg loop {msg.id}: {e}")
            
            # 循环结束后，处理剩余的缓冲区
            if pending_batch:
                await process_batch(pending_batch)
                
        except Exception as e:
            # 频道访问错误（如无权限、频道不存在等）
            logger.error(f"Access error for {channel_name}: {e}")

    async def _forward_message(self, src_channel: str, msgs: list[Message]):
        """
        编排消息转发到所有目标平台 (支持多条消息/相册)

        Args:
            src_channel: 源频道名称
            msgs: Telegram 消息对象列表 (单条消息或相册组)

        Note:
            此方法是转发逻辑的入口点，按顺序调用各平台转发方法
        """
        await self._forward_to_telegram(src_channel, msgs)
        await self._forward_to_qq(src_channel, msgs)

    async def _forward_to_telegram(self, src_channel: str, msgs: list[Message]):
        """
        转发消息到 Telegram 目标频道

        Args:
            src_channel: 源频道名称（用于日志）
            msgs: 要转发的消息对象列表

        转发方式：
            使用 Telethon 的 forward_messages 方法，支持批量转发
        """
        tg_target = self.config.get("target_channel")
        bot_token = self.config.get("bot_token")

        if not msgs: return

        # 只有配置了目标频道和 bot_token 时才转发
        if tg_target and bot_token:
            try:
                 # ========== 解析目标频道 ==========
                 target = tg_target
                 if isinstance(target, str):
                    if target.startswith("-") or target.isdigit():
                        try:
                            target = int(target)
                        except:
                            pass

                 # 获取目标实体并转发消息
                 entity = await self.client.get_entity(target)
                 await self.client.forward_messages(entity, msgs)
                 logger.info(f"Forwarded {len(msgs)} msgs from {src_channel} to TG")
            except Exception as e:
                 logger.error(f"TG Forward Error: {e}")

    async def _forward_to_qq(self, src_channel: str, msgs: list[Message]):
        """
        转发消息到 QQ 群 (支持合并相册)

        Args:
            src_channel: 源频道名称
            msgs: Telegram 消息对象列表

        执行流程：
        1. 遍历所有消息，下载媒体文件
        2. 合并所有消息的文本内容
        3. 构建单一的 NapCat 消息载荷
        4. 发送到 QQ 群
        """
        qq_groups = self.config.get("target_qq_group")
        napcat_url = self.config.get("napcat_api_url")

        if not (qq_groups and napcat_url) or not msgs:
            return
            
        if isinstance(qq_groups, int):
            qq_groups = [qq_groups]
        elif not isinstance(qq_groups, list):
            return

        all_local_files = []
        combined_text_parts = []
        
        try:
            # ========== 1. 遍历消息收集内容 ==========
            for msg in msgs:
                # 收集文本 (去重：如果多张图都有相同caption，只保留一份？或者全部拼接？)
                # 通常相册只有第一张图有caption，或者每张图有不同说明
                # 这里简单策略：全部拼接，用换行符分隔
                if msg.text:
                    cleaned = clean_telegram_text(msg.text)
                    if cleaned:
                        combined_text_parts.append(cleaned)

                # 下载媒体
                files = await self._download_media_safe(msg)
                all_local_files.extend(files)

            # ========== 2. 构建最终文本 ==========
            header = f"From #{src_channel}:\n"
            # 简单去重：如果所有 text 都一样（Telegram 有时会给每张图复制相同 caption），只保留一份
            if len(set(combined_text_parts)) == 1:
                final_body = combined_text_parts[0]
            else:
                final_body = "\n".join(combined_text_parts)
            
            final_text = header + final_body

            # 空内容检查 (既无文本也无文件)
            if not final_body and not all_local_files:
                return

            # ========== 3. 构建消息载荷 ==========
            message = []
            if final_text.strip():
                 message.append({"type": "text", "data": {"text": final_text}})

            # 处理所有收集到的文件
            for fpath in all_local_files:
                file_nodes = await self._process_one_file(fpath)
                if file_nodes:
                    message.extend(file_nodes)
            
            if not message: return

            # ========== 4. 发送 ==========
            url = self.config.get("napcat_api_url", "http://127.0.0.1:3000/send_group_msg")
            async with httpx.AsyncClient() as http:
                 for gid in qq_groups:
                     if not gid: continue
                     try:
                        # 检查是否有 record 节点 (语音特殊处理)
                        has_record = any(node.get("type") == "record" for node in message)
                        
                        if has_record:
                            # 语音拆分发送逻辑 (略微简化，假设相册里很少混语音)
                            text_nodes = [node for node in message if node.get("type") == "text"]
                            if text_nodes:
                                await http.post(url, json={"group_id": gid, "message": text_nodes}, timeout=30)
                                await asyncio.sleep(1)

                            record_nodes = [node for node in message if node.get("type") == "record"]
                            for rec_node in record_nodes:
                                await http.post(url, json={"group_id": gid, "message": [rec_node]}, timeout=30)
                            
                            logger.info(f"Forwarded album/msg to QQ group {gid} (Split)")
                        else:
                            # 普通/相册消息直接发送
                            await http.post(url, json={"group_id": gid, "message": message}, timeout=30)
                            logger.info(f"Forwarded album ({len(msgs)} msgs) to QQ group {gid}")

                     except Exception as e:
                        logger.error(f"Failed to send to QQ group {gid}: {e}")

        except Exception as e:
            logger.error(f"QQ Forward Error: {e}")
        finally:
            # 清理所有临时文件
            self._cleanup_files(all_local_files)

    async def _download_media_safe(self, msg: Message) -> list:
        """
        下载媒体文件（带大小检查）

        Args:
            msg: Telegram 消息对象

        Returns:
            list: 下载的文件路径列表

        安全措施：
            - 文件大小限制：500MB
            - 只下载图片（photo），不下载视频/文档
            - 下载进度回调（每20%输出一次）

        Note:
            为了避免下载大文件导致磁盘空间或性能问题，
            当前只支持图片类型。其他类型会跳过。
        """
        local_files = []

        # 检查消息是否包含媒体
        if not msg.media:
            return local_files

        # ========== 文件大小检查 ==========
        if hasattr(msg.media, 'document') and hasattr(msg.media.document, 'size'):
            if msg.media.document.size > MAX_FILE_SIZE:
                logger.warning(f"File too large ({msg.media.document.size} bytes), skipping download.")
                return local_files

        # ========== 判断是否应该下载 ==========
        # 支持图片和音频
        is_photo = hasattr(msg, 'photo') and msg.photo
        is_audio = False
        
        # 检查音频/语音
        if msg.file and msg.file.mime_type:
            mime = msg.file.mime_type
            if mime.startswith('audio/') or mime == 'application/ogg':
                is_audio = True

        should_download = is_photo or is_audio

        if should_download:
             # 定义进度回调函数
             def progress_callback(current, total):
                if total > 0:
                    pct = (current / total) * 100
                    # 每 20% 输出一次进度，避免日志刷屏
                    if int(pct) % 20 == 0 and int(pct) > 0:
                        logger.info(f"Downloading {msg.id}: {pct:.1f}%")

             # 执行下载
             try:
                path = await self.client.download_media(
                    msg,
                    file=self.plugin_data_dir,
                    progress_callback=progress_callback
                )
                if path:
                    local_files.append(path)
             except asyncio.CancelledError:
                logger.warning(f"Download cancelled for msg {msg.id}")
                return local_files
             except Exception as e:
                logger.error(f"Download failed for msg {msg.id}: {e}")

        return local_files

    async def _process_one_file(self, fpath: str) -> list:
        """
        将本地文件转换为 NapCat 消息节点列表

        Args:
            fpath: 文件路径

        Returns:
            list: NapCat 消息节点列表，每项如 {"type": "image", "data": {...}}

        处理策略：
            1. 图片文件（<5MB）：使用 Base64 编码直接嵌入
            2. 音频文件：上传后生成 [语音消息 + 链接]
            3. 其他文件：上传到文件托管服务（如果配置）
            4. 无托管：返回文件名占位符
        """
        ext = os.path.splitext(fpath)[1].lower()
        hosting_url = self.config.get("file_hosting_url")

        # ========== 1. 图片 -> Base64（小文件安全） ==========
        if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
             # 检查文件大小，Base64 对大文件不友好
            if os.path.getsize(fpath) < 5 * 1024 * 1024:
                import base64
                with open(fpath, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                # NapCat 图片消息格式
                return [{"type": "image", "data": {"file": f"base64://{encoded_string}"}}]
            else:
                logger.info("Image too large for base64, trying upload...")

        # ========== 2. 上传到文件托管服务 ==========
        if hosting_url:
            try:
                link = await self.uploader.upload(fpath, hosting_url)
                
                if link:
                    # 如果是音频，尝试发送语音预览 + 链接
                    if ext in [".mp3", ".ogg", ".wav", ".m4a", ".flac", ".amr"]:
                            logger.info(f"Audio Link Generated: {link}")
                            return [
                                {"type": "text", "data": {"text": f"\n[Audio: {os.path.basename(fpath)}]\n🔗 Link: {link}\n"}},
                                {"type": "record", "data": {"file": link}}
                            ]
                    
                    # 普通文件/大图片
                    return [{"type": "text", "data": {"text": f"\n[Media Link: {link}]"}}]
                else:
                     return [{"type": "text", "data": {"text": f"\n[Media File: {os.path.basename(fpath)}] (Upload Failed)"}}]
            except Exception as e:
                 logger.error(f"Upload Error: {type(e).__name__}: {e}")
                 return [{"type": "text", "data": {"text": f"\n[Media File: {os.path.basename(fpath)}] (Upload Failed)"}}]


        # ========== 3. 回退方案 ==========
        # 无托管服务时，返回文件名占位符
        fname = os.path.basename(fpath)
        return [{"type": "text", "data": {"text": f"\n[Media File: {fname}] (Too large/No hosting)"}}]

    def _cleanup_files(self, files: list):
        """
        清理临时下载的文件

        Args:
            files: 文件路径列表

        行为：
            - 删除每个存在的临时文件
            - 静默处理删除失败（文件可能已被其他进程占用）
        """
        for f in files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    # 删除失败时静默忽略，不中断流程
                    pass

    def _cleanup_orphaned_files(self):
        """
        启动时清理插件数据目录中的孤儿文件
        
        保留文件：
        - data.json (持久化数据)
        - user_session.session (Telethon 会话)
        - user_session.session-journal (SQLite 临时文件)
        
        删除文件：
        - 所有其他文件（主要是残留的媒体文件）
        """
        if not os.path.exists(self.plugin_data_dir):
            return

        logger.info(f"Cleaning up orphaned files in {self.plugin_data_dir}...")
        
        allowlist = ["data.json", "user_session.session", "user_session.session-journal"]
        deleted_count = 0
        
        try:
            for filename in os.listdir(self.plugin_data_dir):
                if filename in allowlist:
                    continue
                    
                file_path = os.path.join(self.plugin_data_dir, filename)
                
                # 只删除文件，不删除子目录（虽然现在没有子目录）
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                        logger.debug(f"Deleted orphaned file: {filename}")
                    except Exception as e:
                        logger.warning(f"Failed to delete {filename}: {e}")
            
            if deleted_count > 0:
                logger.info(f"Cleanup finished. Removed {deleted_count} orphaned files.")
            else:
                logger.info("Cleanup finished. No orphaned files found.")
                
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
