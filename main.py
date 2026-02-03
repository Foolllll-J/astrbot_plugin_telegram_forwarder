import asyncio
import os
import shutil
import filecmp
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger, star, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import StarTools

from .common.storage import Storage
from .core.client import TelegramClientWrapper
from .core.forwarder import Forwarder
from .core.commands import PluginCommands


class Main(star.Star):
    """
    Telegram 转发插件主类

    继承自 AstrBot 的 star.Star 基类，实现插件的生命周期管理。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """
        插件初始化
        """
        super().__init__(context)
        self.context = context
        self.config = config
        self.bot = None # 初始化 bot 属性，防止 on_qq_message 报错


        # ========== 设置数据目录 ==========
        self.plugin_data_dir = str(StarTools.get_data_dir())
        if not os.path.exists(self.plugin_data_dir):
            os.makedirs(self.plugin_data_dir)

        # ========== 数据迁移 (Legacy -> New) ==========
        # 旧位置: 插件源码目录 (e.g. data/plugins/astrbot_plugin_telegram_forwarder)
        legacy_dir = os.path.dirname(__file__)
        files_to_migrate = [
            "data.json",
            "user_session.session",
            "user_session.session-journal",
        ]

        for filename in files_to_migrate:
            src = os.path.join(legacy_dir, filename)
            dst = os.path.join(self.plugin_data_dir, filename)

            if os.path.exists(src):
                # 如果目标不存在，或者目标也是空的，才迁移
                should_migrate = False
                if not os.path.exists(dst):
                    should_migrate = True
                elif (
                    filename == "data.json" and os.path.getsize(dst) < 100
                ):  # 可能是空的默认文件
                    should_migrate = True

                if should_migrate:
                    try:
                        shutil.copy2(src, dst)
                        logger.debug(f"[Migration] 已将 {filename} 从插件目录迁移到数据目录。")
                    except Exception as e:
                        logger.error(f"[Migration] 迁移 {filename} 失败: {e}")

        # ========== 初始化核心组件 ==========
        self.storage = Storage(os.path.join(self.plugin_data_dir, "data.json"))

        # ========== 处理上传的 Session 文件 ==========
        session_files = self.config.get("telegram_session", [])
        if session_files and isinstance(session_files, list) and len(session_files) > 0:
            uploaded_session_path = session_files[0]
            full_uploaded_path = os.path.join(self.plugin_data_dir, uploaded_session_path)

            if os.path.exists(full_uploaded_path):
                target_session_path = os.path.join(self.plugin_data_dir, "user_session.session")
                
                should_copy = True
                if os.path.exists(target_session_path):
                    try:
                        if filecmp.cmp(full_uploaded_path, target_session_path, shallow=False):
                            should_copy = False
                            logger.debug("[Main] 会话文件未变化，跳过同步。")
                    except Exception as e:
                        logger.warning(f"[Main] 比较会话文件失败: {e}")

                if should_copy:
                    try:
                        shutil.copy2(full_uploaded_path, target_session_path)
                        logger.debug(f"[Main] 已从上传配置同步会话文件: {target_session_path}")
                        
                        from .core.client import get_client_cache
                        cache = get_client_cache()
                        if target_session_path in cache:
                            logger.debug("[Main] 会话文件已更新，清理缓存以强制重新连接。")
                            del cache[target_session_path]
                            
                    except Exception as e:
                        logger.error(f"[Main] 同步会话文件失败 (可能被占用): {e}")
            else:
                logger.warning(f"[Main] 配置中的会话文件路径不存在: {full_uploaded_path}")

        # TelegramClientWrapper: 封装 Telegram 客户端连接逻辑
        self.client_wrapper = TelegramClientWrapper(self.config, self.plugin_data_dir)

        # Forwarder: 消息转发核心逻辑
        self.forwarder = Forwarder(
            self.context, self.config, self.storage, self.client_wrapper, self.plugin_data_dir
        )

        # ========== 初始化定时任务调度器 ==========
        self.scheduler = AsyncIOScheduler()

        # 初始化命令处理器
        self.command_handler = PluginCommands(context, config, self.forwarder)

        # ========== 配置检查警告 ==========
        if not self.config.get("api_id") or not self.config.get("api_hash"):
            logger.warning(
                "Telegram Forwarder: 缺少 api_id 或 api_hash，请在配置中填写。"
            )

    async def initialize(self):
        """
        插件启动逻辑
        
        执行流程：
        1. 启动 Telegram 客户端连接
        2. 如果连接成功且插件已启用，启动定时任务
        3. 定时任务按照配置的间隔检查频道更新
        """
        # 启动 Telegram 客户端（处理登录、会话恢复等）
        # 如果配置了 api_id 和 api_hash，尝试启动
        if self.client_wrapper.client:
            await self.client_wrapper.start()

        # 检查客户端是否成功连接并授权
        if self.client_wrapper.is_authorized():
            # ========== 启动定时调度器 ==========
            # 目前采用一体化 FGSS 模式，只需一个任务即可
            send_interval = self.config.get("check_interval", 60) # 使用全局检查间隔
            self.scheduler.add_job(
                self.forwarder.check_updates,
                "interval",
                seconds=send_interval,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now() + timedelta(seconds=60) # 首次执行稍作延迟
            )

            # 启动调度器
            self.scheduler.start()

            logger.info(f"Telegram Forwarder 已启动。检查间隔: {send_interval}s")
            logger.debug(f"正在监控频道: {self.config.get('source_channels')}")

        # 捕获 QQ 平台实例变量初始化 (已在 __init__ 中处理)
        # self.bot = None

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL)
    async def on_qq_message(self, event: AstrMessageEvent):
        """通过接收到的消息动态捕获并确认 QQ 平台 ID"""
        umo = event.unified_msg_origin
        if ":" in umo:
            platform_id = umo.split(":")[0]
            if not self.bot or getattr(self.forwarder.qq_sender, "platform_id", None) != platform_id:
                self.bot = event.bot
                if hasattr(self, "forwarder") and hasattr(self.forwarder, "qq_sender"):
                    self.forwarder.qq_sender.platform_id = platform_id
                    self.forwarder.qq_sender.bot = event.bot
                logger.debug(f"通过消息事件成功捕获/更新 QQ platform_id: {platform_id}")
                
                if hasattr(self.forwarder.qq_sender, "_ensure_node_name"):
                    asyncio.create_task(self.forwarder.qq_sender._ensure_node_name(event.bot))

    async def terminate(self):
        """插件终止时的清理工作"""
        logger.debug("[Main] 正在停止插件...")

        # 1. Stop Scheduler
        if self.scheduler.running:
            logger.debug("[Main] 正在关闭调度器...")
            self.scheduler.shutdown(wait=False)
            logger.debug("[Main] 调度器已关闭。")

        # 2. Client Disconnect Strategy
        force_disconnect = self.config.get("force_disconnect", False)

        if self.client_wrapper.client:
            if force_disconnect:
                logger.debug("[Main] 正在强制断开客户端连接...")
                try:
                    await asyncio.wait_for(
                        self.client_wrapper.client.disconnect(), timeout=1.0
                    )
                    logger.debug("[Main] 客户端已断开连接。")
                    from .core.client import TelegramClientWrapper
                    TelegramClientWrapper.clear_cache()
                except asyncio.TimeoutError:
                    logger.warning("[Main] 断开客户端连接超时")
                except Exception as e:
                    logger.error(f"[Main] 断开客户端连接时出错: {e}")
            else:
                logger.debug("[Main] 保持客户端连接以便快速重载")

        logger.info("Telegram Forwarder 已停止")

    # ================= COMMANDS =================

    @filter.command_group("tg")
    def tg(self):
        """Telegram Forwarder 插件管理"""
        pass

    @tg.command("add")
    async def add_channel(self, event: AstrMessageEvent, channel: str):
        """添加监控频道: /tg add <channel>"""
        async for result in self.command_handler.add_channel(event, channel):
            yield result

    @tg.command("rm")
    async def remove_channel(self, event: AstrMessageEvent, channel: str):
        """移除监控频道: /tg rm <channel>"""
        async for result in self.command_handler.remove_channel(event, channel):
            yield result

    @tg.command("ls")
    async def list_channels(self, event: AstrMessageEvent):
        """列出所有监控频道: /tg ls"""
        async for result in self.command_handler.list_channels(event):
            yield result

    @tg.command("check")
    async def force_check(self, event: AstrMessageEvent):
        """立即检查更新: /tg check"""
        async for result in self.command_handler.force_check(event):
            yield result

    @tg.command("help")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        async for result in self.command_handler.show_help(event):
            yield result
