
from typing import List
from telethon.tl.types import Message
from astrbot.api import logger, AstrBotConfig

class TelegramSender:
    """
    负责将消息转发到 Telegram 目标频道
    """
    def __init__(self, client, config: AstrBotConfig):
        self.client = client
        self.config = config

    async def send(self, msgs: List[Message], src_channel: str):
        """
        转发消息到 Telegram 目标频道

        Args:
            msgs: 要转发的消息对象列表
            src_channel: 源频道名称（用于日志）
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
                 target_entity = await self.client.get_entity(target)
                 await self.client.forward_messages(target_entity, msgs)
                 logger.info(f"Forwarded {len(msgs)} msgs from {src_channel} to TG")
            except Exception as e:
                 logger.error(f"TG Forward Error: {e}")
