import os
import asyncio
import httpx
from typing import List
from telethon.tl.types import Message
from astrbot.api import logger, AstrBotConfig, star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, Record, Video, Node, Nodes

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
        self._group_locks = {}  # 群锁，防止并发发送
        self.platform_id = None # 动态捕获的平台 ID
        self.bot = None         # 动态捕获的 bot 实例
        self.node_name = None   # 合并转发消息时显示的 bot 昵称

    async def _ensure_node_name(self, bot):
        """获取 bot 昵称"""
        if self.node_name:
            return self.node_name
        
        try:
            # 优先从登录信息获取
            info = await bot.get_login_info()
            if info and (nickname := info.get("nickname")):
                self.node_name = str(nickname)
                logger.debug(f"[QQSender] 获取到 bot 昵称: {self.node_name}")
            else:
                logger.debug(f"[QQSender] 未能从登录信息获取到昵称")
        except Exception as e:
            logger.debug(f"[QQSender] 获取 bot 昵称异常: {e}")
            
        if not self.node_name:
            self.node_name = "AstrBot"
        return self.node_name

    def _get_lock(self, group_id):
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    async def send(self, batches: List[List[Message]], src_channel: str):
        """
        转发消息到 QQ 群
        """
        qq_groups = self.config.get("target_qq_group")
        napcat_url = self.config.get("napcat_api_url")
        exclude_text_on_media = self.config.get("exclude_text_on_media", False)

        if not qq_groups or not napcat_url or not batches:
            return

        if isinstance(qq_groups, int):
            qq_groups = [qq_groups]
        elif not isinstance(qq_groups, list):
            return

        url = napcat_url if napcat_url else "http://127.0.0.1:3000/send_group_msg"
        is_localhost = url.lower() == "localhost"

        if is_localhost:
            qq_platform_id = self.platform_id
            if not qq_platform_id:
                logger.warning("[QQSender] Localhost 模式下尚未捕获到有效的 QQ 平台 ID，跳过本次转发。")
                return

            bot = self.bot
            
            if not bot:
                try:
                    platform = self.context.get_platform(qq_platform_id)
                    if platform:
                        bot = platform.bot
                    
                    if not bot:
                        all_platforms = self.context.get_all_platforms()
                        if all_platforms:
                            for p in all_platforms:
                                if hasattr(p, "platform_config") and p.platform_config.get("id") == qq_platform_id:
                                    bot = p.bot
                                    break
                    
                    if not bot:
                        logger.warning(f"[QQSender] 无法通过 platform_id '{qq_platform_id}' 获取到有效 bot 实例。")
                except Exception as e:
                    logger.error(f"[QQSender] 获取 bot 实例失败: {e}")
            
            self_id = 0
            node_name = "AstrBot"
            if bot:
                try:
                    node_name = await self._ensure_node_name(bot)
                    info = await bot.get_login_info()
                    self_id = info.get("user_id", 0)
                except Exception as e:
                    logger.error(f"[QQSender] 获取 bot 详细信息失败: {e}")
            else:
                logger.warning(f"[QQSender] 未获取到 bot 实例，将使用默认名称 '{node_name}'")

            for gid in qq_groups:
                if not gid:
                    continue
                
                lock = self._get_lock(gid)
                async with lock:
                    for msgs in batches:
                        all_local_files = []
                        all_nodes_data = [] 
                        
                        try:
                            header = f"From #{src_channel}:"
                            
                            for i, msg in enumerate(msgs):
                                current_node_components = []
                                
                                # 处理文本
                                text_parts = []
                                if msg.text:
                                    cleaned = clean_telegram_text(msg.text)
                                    if cleaned:
                                        text_parts.append(cleaned)
                                
                                # 处理媒体
                                media_components = []
                                files = await self.downloader.download_media(msg)
                                for fpath in files:
                                    all_local_files.append(fpath)
                                    ext = os.path.splitext(fpath)[1].lower()
                                    if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                                        media_components.append(Image.fromFileSystem(fpath))
                                    elif ext in [".mp3", ".ogg", ".wav", ".m4a", ".flac", ".amr"]:
                                        media_components.append(Record.fromFileSystem(fpath))
                                    elif ext in [".mp4", ".mov", ".avi", ".mkv", ".flv"]:
                                        media_components.append(Video.fromFileSystem(fpath))
                                    else:
                                        media_components.append(Plain(f"\n[文件: {os.path.basename(fpath)}]"))

                                has_media = len(media_components) > 0
                                should_exclude_text = exclude_text_on_media and has_media

                                if i == 0 and not should_exclude_text:
                                    if text_parts:
                                        text_parts[0] = f"{header}\n\u200b{text_parts[0]}"
                                    else:
                                        current_node_components.append(Plain(f"{header}\n\u200b"))

                                if not should_exclude_text:
                                    for t in text_parts:
                                        current_node_components.append(Plain(t + "\n"))
                                
                                current_node_components.extend(media_components)
                                
                                if current_node_components:
                                    is_only_header = (i == 0 and len(current_node_components) == 1 and 
                                                     isinstance(current_node_components[0], Plain) and 
                                                     current_node_components[0].text in [header, header + "\n", f"{header}\n\u200b"])
                                    
                                    if not is_only_header:
                                        all_nodes_data.append(current_node_components)

                            if not all_nodes_data:
                                continue

                            message_chain = MessageChain()
                            if len(all_nodes_data) > 1:
                                nodes_list = []
                                for node_content in all_nodes_data:
                                    nodes_list.append(Node(uin=self_id, name=node_name, content=node_content))
                                
                                message_chain.chain.append(Nodes(nodes_list))
                                log_msg = f"[QQSender] Bot({node_name}) 合并转发相册 ({len(all_nodes_data)} 节点) 到群 {gid}"
                            else:
                                message_chain.chain.extend(all_nodes_data[0])
                                log_msg = f"[QQSender] Bot({node_name}) 转发单条消息到群 {gid}"

                            unified_msg_origin = f"{qq_platform_id}:GroupMessage:{gid}"
                            await self.context.send_message(unified_msg_origin, message_chain)
                            logger.info(log_msg)
                            
                            await asyncio.sleep(1)

                        except Exception as e:
                            logger.error(f"[QQSender] AstrBot 转发异常: {e}")
                        finally:
                            self._cleanup_files(all_local_files)
        else:
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
                                for msg in msgs:
                                    if msg.text:
                                        cleaned = clean_telegram_text(msg.text)
                                        if cleaned:
                                            combined_text_parts.append(cleaned)
                                    files = await self.downloader.download_media(msg)
                                    all_local_files.extend(files)

                                header = f"From #{src_channel}:\n"
                                if len(set(combined_text_parts)) == 1:
                                    final_body = combined_text_parts[0]
                                else:
                                    final_body = "\n".join(combined_text_parts)

                                final_text = header + final_body
                                if not final_body and not all_local_files:
                                    continue

                                message = []
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

                                try:
                                    has_record = any(node.get("type") == "record" for node in message)
                                    if has_record:
                                        text_nodes = [node for node in message if node.get("type") == "text"]
                                        if text_nodes:
                                            await http.post(url, json={"group_id": gid, "message": text_nodes}, timeout=60)
                                        record_nodes = [node for node in message if node.get("type") == "record"]
                                        for rec_node in record_nodes:
                                            await http.post(url, json={"group_id": gid, "message": [rec_node]}, timeout=60)
                                        logger.info(f"[QQSender] 转发语音消息到群 {gid}")
                                    else:
                                        await http.post(url, json={"group_id": gid, "message": message}, timeout=60)
                                        logger.info(f"[QQSender] 转发相册/消息 ({len(msgs)} 条) 到群 {gid}")
                                    
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    logger.error(f"[QQSender] 发送到群 {gid} 失败: {e}")
                            
                            except Exception as e:
                                logger.error(f"[QQSender] 批次处理异常: {e}")
                            finally:
                                self._cleanup_files(all_local_files)

    async def _process_one_file(self, fpath: str) -> List[dict]:
        """
        将本地文件转换为 NapCat 消息节点列表
        """
        ext = os.path.splitext(fpath)[1].lower()
        hosting_url = self.config.get("file_hosting_url")

        # 1. 处理图片：50MB 以下尝试 Base64 发送
        if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
            if os.path.getsize(fpath) < 50 * 1024 * 1024:
                try:
                    import base64
                    with open(fpath, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                    return [
                        {
                            "type": "image",
                            "data": {"file": f"base64://{encoded_string}"},
                        }
                    ]
                except Exception as e:
                    logger.debug(f"[QQSender] 图片转 Base64 失败: {e}")
            else:
                logger.debug(f"[QQSender] 图片过大，尝试其他方式发送")

        # 2. 上传到文件托管服务
        if hosting_url:
            try:
                link = await self.uploader.upload(fpath, hosting_url)

                if link:
                    # 音频文件发送语音节点
                    if ext in [".mp3", ".ogg", ".wav", ".m4a", ".flac", ".amr"]:
                        return [
                            {
                                "type": "text",
                                "data": {
                                    "text": f"\n[音频: {os.path.basename(fpath)}]\n🔗 链接: {link}\n"
                                },
                            },
                            {"type": "record", "data": {"file": link}},
                        ]

                    # 其他媒体文件返回链接
                    return [
                        {"type": "text", "data": {"text": f"\n[媒体链接: {link}]"}}
                    ]
                else:
                    return [
                        {
                            "type": "text",
                            "data": {
                                "text": f"\n[媒体文件: {os.path.basename(fpath)}] (上传失败)"
                            },
                        }
                    ]
            except Exception as e:
                logger.error(f"[QQSender] 上传失败: {e}")
                return [
                    {
                        "type": "text",
                        "data": {
                            "text": f"\n[媒体文件: {os.path.basename(fpath)}] (上传异常)"
                        },
                    }
                ]

        # 3. 回退方案
        fname = os.path.basename(fpath)
        return [
            {
                "type": "text",
                "data": {"text": f"\n[媒体文件: {fname}] (文件过大或未配置托管)"},
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
