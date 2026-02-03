import os
import asyncio
import httpx
from typing import List
from telethon.tl.types import Message
from astrbot.api import logger, AstrBotConfig, star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, Record, Video

from ...common.text_tools import clean_telegram_text
from ..downloader import MediaDownloader
from ..uploader import FileUploader


class QQSender:
    """
    负责将消息转发到 QQ 群 (支持合并相册)
    """

    def __init__(
        self, context: star.Context, config: AstrBotConfig, downloader: MediaDownloader, uploader: FileUploader
    ):
        self.context = context
        self.config = config
        self.downloader = downloader
        self.uploader = uploader
        self._group_locks = {}  # simple dict
        self.platform_id = None # 动态捕获的平台 ID

    def _get_lock(self, group_id):
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    async def send(self, batches: List[List[Message]], src_channel: str):
        """
        转发消息到 QQ 群

        Args:
            batches: 消息批次列表 (List[List[Message]])
            src_channel: 源频道名称
        """
        qq_groups = self.config.get("target_qq_group")
        napcat_url = self.config.get("napcat_api_url")
        exclude_text_on_media = self.config.get("exclude_text_on_media", False)

        # 检查是否配置了 QQ 群，如果没有配置则认为不启用 QQ 转发
        if not qq_groups or not napcat_url or not batches:
            return

        if isinstance(qq_groups, int):
            qq_groups = [qq_groups]
        elif not isinstance(qq_groups, list):
            return

        # 使用配置的 URL 或默认值
        url = napcat_url if napcat_url else "http://127.0.0.1:3000/send_group_msg"
        is_localhost = url.lower() == "localhost"

        if is_localhost:
            # 必须使用动态捕获的 platform_id，如果没有捕获到则跳过发送
            qq_platform_id = self.platform_id
            if not qq_platform_id:
                logger.warning("Localhost 模式下尚未捕获到有效的 QQ 平台 ID，跳过本次转发。")
                return

            for gid in qq_groups:
                if not gid:
                    continue
                
                lock = self._get_lock(gid)
                async with lock:
                    for msgs in batches:
                        all_local_files = []
                        combined_text_parts = []
                        
                        try:
                            # ========== 1. 遍历消息收集内容 ==========
                            for msg in msgs:
                                if msg.text:
                                    cleaned = clean_telegram_text(msg.text)
                                    if cleaned:
                                        combined_text_parts.append(cleaned)
                                files = await self.downloader.download_media(msg)
                                all_local_files.extend(files)

                            # ========== 2. 构建最终文本 ==========
                            header = f"From #{src_channel}:\n"
                            if len(set(combined_text_parts)) == 1:
                                final_body = combined_text_parts[0]
                            else:
                                final_body = "\n".join(combined_text_parts)

                            final_text = header + final_body
                            if not final_body and not all_local_files:
                                continue

                            # ========== 3. 构建 AstrBot 消息链 ==========
                            message_chain = MessageChain()
                            
                            # 如果配置了媒体消息排除文本，且确实有媒体，则不添加文本
                            if exclude_text_on_media and all_local_files:
                                pass
                            elif final_text.strip():
                                message_chain.chain.append(Plain(final_text))

                            for fpath in all_local_files:
                                ext = os.path.splitext(fpath)[1].lower()
                                if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                                    message_chain.chain.append(Image.fromFileSystem(fpath))
                                elif ext in [".mp3", ".ogg", ".wav", ".m4a", ".flac", ".amr"]:
                                    message_chain.chain.append(Record.fromFileSystem(fpath))
                                elif ext in [".mp4", ".mov", ".avi", ".mkv", ".flv"]:
                                    # 视频文件使用 Video 组件
                                    message_chain.chain.append(Video.fromFileSystem(fpath))
                                else:
                                    # 其他文件类型暂不支持直接发送，可以发送一个提示
                                    message_chain.chain.append(Plain(f"\n[File: {os.path.basename(fpath)}]"))

                            if not message_chain.chain:
                                continue

                            # ========== 4. 发送 ==========
                            unified_msg_origin = f"{qq_platform_id}:GroupMessage:{gid}"
                            await self.context.send_message(unified_msg_origin, message_chain)
                            logger.info(f"Forwarded album ({len(msgs)} msgs) to QQ group {gid} via AstrBot API")
                            
                            await asyncio.sleep(1)

                        except Exception as e:
                            logger.error(f"AstrBot API Forward Error processing batch: {e}")
                        finally:
                            self._cleanup_files(all_local_files)
        else:
            # 原有的 NapCat HTTP API 发送逻辑
            async with httpx.AsyncClient() as http:
                for gid in qq_groups:
                    if not gid:
                        continue
                    
                    lock = self._get_lock(gid)
                    async with lock:
                        for msgs in batches:
                            all_local_files = []
                            combined_text_parts = []
                            
                            try:
                                # ========== 1. 遍历消息收集内容 ==========
                                for msg in msgs:
                                    if msg.text:
                                        cleaned = clean_telegram_text(msg.text)
                                        if cleaned:
                                            combined_text_parts.append(cleaned)
                                    files = await self.downloader.download_media(msg)
                                    all_local_files.extend(files)

                                # ========== 2. 构建最终文本 ==========
                                header = f"From #{src_channel}:\n"
                                if len(set(combined_text_parts)) == 1:
                                    final_body = combined_text_parts[0]
                                else:
                                    final_body = "\n".join(combined_text_parts)

                                final_text = header + final_body
                                if not final_body and not all_local_files:
                                    continue

                                # ========== 3. 构建消息载荷 ==========
                                message = []
                                
                                # 如果配置了媒体消息排除文本，且确实有媒体，则不添加文本
                                if exclude_text_on_media and all_local_files:
                                    pass
                                elif final_text.strip():
                                    message.append({"type": "text", "data": {"text": final_text}})

                                for fpath in all_local_files:
                                    file_nodes = await self._process_one_file(fpath)
                                    if file_nodes:
                                        message.extend(file_nodes)

                                if not message:
                                    continue

                                # ========== 4. 发送 ==========
                                try:
                                    has_record = any(node.get("type") == "record" for node in message)
                                    if has_record:
                                        text_nodes = [node for node in message if node.get("type") == "text"]
                                        if text_nodes:
                                            await http.post(url, json={"group_id": gid, "message": text_nodes}, timeout=60)
                                        record_nodes = [node for node in message if node.get("type") == "record"]
                                        for rec_node in record_nodes:
                                            await http.post(url, json={"group_id": gid, "message": [rec_node]}, timeout=60)
                                        logger.info(f"Forwarded album/msg to QQ group {gid} (Split without delay)")
                                    else:
                                        await http.post(url, json={"group_id": gid, "message": message}, timeout=60)
                                        logger.info(f"Forwarded album ({len(msgs)} msgs) to QQ group {gid}")
                                    
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    logger.error(f"Failed to send to QQ group {gid}: {type(e).__name__}: {e}")
                            
                            except Exception as e:
                                logger.error(f"QQ Forward Error processing batch: {e}")
                            finally:
                                self._cleanup_files(all_local_files)

    async def _process_one_file(self, fpath: str) -> List[dict]:
        """
        将本地文件转换为 NapCat 消息节点列表
        """
        ext = os.path.splitext(fpath)[1].lower()
        hosting_url = self.config.get("file_hosting_url")

        # ========== 1. 图片 -> Base64（小文件安全） ==========
        if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
            # 50MB limit for Base64 (approx 66MB string)
            if os.path.getsize(fpath) < 50 * 1024 * 1024:
                try:
                    import base64

                    with open(fpath, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode(
                            "utf-8"
                        )
                    return [
                        {
                            "type": "image",
                            "data": {"file": f"base64://{encoded_string}"},
                        }
                    ]
                except Exception as e:
                    logger.warning(f"Base64 convert failed: {e}")
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
                            {
                                "type": "text",
                                "data": {
                                    "text": f"\n[Audio: {os.path.basename(fpath)}]\n🔗 Link: {link}\n"
                                },
                            },
                            {"type": "record", "data": {"file": link}},
                        ]

                    # 普通文件/大图片/视频
                    return [
                        {"type": "text", "data": {"text": f"\n[Media Link: {link}]"}}
                    ]
                else:
                    return [
                        {
                            "type": "text",
                            "data": {
                                "text": f"\n[Media File: {os.path.basename(fpath)}] (Upload Failed)"
                            },
                        }
                    ]
            except Exception as e:
                logger.error(f"Upload Error: {type(e).__name__}: {e}")
                return [
                    {
                        "type": "text",
                        "data": {
                            "text": f"\n[Media File: {os.path.basename(fpath)}] (Upload Failed)"
                        },
                    }
                ]

        # ========== 3. 回退方案 ==========
        fname = os.path.basename(fpath)
        return [
            {
                "type": "text",
                "data": {"text": f"\n[Media File: {fname}] (Too large/No hosting)"},
            }
        ]

    def _cleanup_files(self, files: List[str]):
        """清理临时下载的文件"""
        for f in files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass
