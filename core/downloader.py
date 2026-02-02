import os
import asyncio
from typing import List, Optional, Callable
from telethon.tl.types import Message
from astrbot.api import logger


class MediaDownloader:
    """
    负责从 Telegram 消息中下载媒体文件
    """

    def __init__(
        self, client, plugin_data_dir: str, max_file_size: int = 500 * 1024 * 1024
    ):
        self.client = client
        self.plugin_data_dir = plugin_data_dir
        self.max_file_size = max_file_size

    async def download_media(self, msg: Message) -> List[str]:
        """
        下载媒体文件（带大小检查）

        Args:
            msg: Telegram 消息对象

        Returns:
            list: 下载的文件路径列表
        """
        local_files = []

        # 检查消息是否包含媒体
        if not msg.media:
            return local_files

        # ========== 文件大小检查 ==========
        if hasattr(msg.media, "document") and hasattr(msg.media.document, "size"):
            if msg.media.document.size > self.max_file_size:
                logger.warning(
                    f"File too large ({msg.media.document.size} bytes), skipping download."
                )
                return local_files

        # ========== 判断是否应该下载 ==========
        # 支持图片、音频和视频
        is_photo = hasattr(msg, "photo") and msg.photo
        is_video = False
        is_audio = False

        # 检查视频
        if msg.video:
            is_video = True
        
        # 检查音频/语音
        if msg.file and msg.file.mime_type:
            mime = msg.file.mime_type
            if mime.startswith("audio/") or mime == "application/ogg":
                is_audio = True
            elif mime.startswith("video/"):
                is_video = True

        should_download = is_photo or is_audio or is_video

        # 检查是否为图片/视频文档 (原文件发送)
        if not should_download and msg.file and msg.file.mime_type:
            if msg.file.mime_type.startswith("image/") or msg.file.mime_type.startswith("video/"):
                should_download = True

        if should_download:
            media_type = "Photo" if is_photo else ("Video" if is_video else "Audio")
            logger.info(f"Detected {media_type} in msg {msg.id}, starting download...")
            # 定义进度回调函数
            def progress_callback(current, total):
                if total > 0:
                    pct = (current / total) * 100
                    # 每 20% 输出一次进度, 避免日志刷屏
                    if int(pct) % 20 == 0 and int(pct) > 0:
                        logger.info(f"Downloading {msg.id}: {pct:.1f}%")

            # 执行下载
            retry_count = 3
            for attempt in range(retry_count):
                try:
                    if not self.client.is_connected():
                        logger.warning(
                            f"Client disconnected during download (attempt {attempt+1}), trying to reconnect..."
                        )
                        try:
                            await self.client.connect()
                        except Exception as e:
                            logger.error(f"Reconnection failed: {e}")

                    path = await self.client.download_media(
                        msg,
                        file=self.plugin_data_dir,
                        progress_callback=progress_callback,
                    )
                    if path:
                        local_files.append(path)
                        break  # Success
                except asyncio.CancelledError:
                    logger.warning(f"Download cancelled for msg {msg.id}")
                    return local_files  # Don't retry on cancel
                except Exception as e:
                    logger.warning(
                        f"Download failed for msg {msg.id} (Attempt {attempt+1}/{retry_count}): {e}"
                    )
                    if attempt < retry_count - 1:
                        await asyncio.sleep(2)
                    else:
                        logger.error(f"Download permanently failed for msg {msg.id}")

        return local_files
