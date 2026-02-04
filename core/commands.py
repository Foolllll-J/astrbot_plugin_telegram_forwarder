import asyncio
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context
from astrbot.api import AstrBotConfig


class PluginCommands:
    def __init__(self, context: Context, config: AstrBotConfig, forwarder):
        self.context = context
        self.config = config
        self.forwarder = forwarder

    async def add_channel(self, event: AstrMessageEvent, channel: str):
        """添加监控频道"""
        if not channel:
            yield event.plain_result("❌ 请指定频道名称，例如: /tg add channel_name")
            return

        channels = self.config.get("source_channels", [])
        
        exists = False
        for c in channels:
            if isinstance(c, dict) and c.get("channel_username") == channel:
                exists = True
                break
            elif isinstance(c, str) and c == channel:
                exists = True
                break
        
        if exists:
            yield event.plain_result(f"⚠️ 频道 {channel} 已经在监控列表中。")
            return

        new_item = {
            "__template_key": "default",
            "channel_username": channel,
            "start_time": "",
            "check_interval": 60,
            "msg_limit": 10
        }
        channels.append(new_item)
        self.config["source_channels"] = channels
        self.config.save_config()
        yield event.plain_result(f"✅ 已添加频道 {channel} 到监控列表。")

    async def remove_channel(self, event: AstrMessageEvent, channel: str):
        """移除监控频道"""
        if not channel:
            yield event.plain_result("❌ 请指定频道名称，例如: /tg rm channel_name")
            return

        channels = self.config.get("source_channels", [])
        
        target_index = -1
        for i, c in enumerate(channels):
            if isinstance(c, dict) and c.get("channel_username") == channel:
                target_index = i
                break
            elif isinstance(c, str) and c == channel:
                target_index = i
                break

        if target_index == -1:
            yield event.plain_result(f"⚠️ 频道 {channel} 不在监控列表中。")
            return

        channels.pop(target_index)
        self.config["source_channels"] = channels
        self.config.save_config()
        yield event.plain_result(f"✅ 已移除频道 {channel}。")

    async def list_channels(self, event: AstrMessageEvent):
        """列出所有监控频道"""
        channels = self.config.get("source_channels", [])
        if not channels:
            yield event.plain_result("📭 当前没有监控任何频道。")
            return

        display_list = []
        for c in channels:
            if isinstance(c, dict):
                name = c.get("channel_username", "Unknown")
                s_time = c.get("start_time", "实时")
                if not s_time: s_time = "实时"
                display_list.append(f"- {name} ({s_time})")
            else:
                display_list.append(f"- {c}")

        msg = "📺 当前监控的频道列表:\n" + "\n".join(display_list)
        yield event.plain_result(msg)

    async def force_check(self, event: AstrMessageEvent):
        """立即检查更新"""
        yield event.plain_result("🔄 正在触发立即检查更新并尝试发送...")
        
        async def run_sync():
            await self.forwarder.check_updates()
            await self.forwarder.send_pending_messages()
            
        asyncio.create_task(run_sync())

    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = (
            "🤖 Telegram Forwarder 帮助\n"
            "--------------------------\n"
            "/tg add <channel>  - 添加监控频道\n"
            "/tg rm <channel>   - 移除监控频道\n"
            "/tg ls             - 列出所有监控频道\n"
            "/tg check          - 立即检查更新\n"
            "/tg help           - 显示此帮助"
        )
        yield event.plain_result(help_text)
