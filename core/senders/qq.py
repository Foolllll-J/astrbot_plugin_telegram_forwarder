import os
import asyncio
from typing import List, Optional, Iterable
from telethon.tl.types import Message
from astrbot.api import logger, AstrBotConfig, star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    Plain,
    Image,
    Record,
    Video,
    Node,
    Nodes,
    File,
)

try:
    from astrbot.core.utils.path_util import path_Mapping
except ImportError:
    path_Mapping = None

from ...common.text_tools import clean_telegram_text, is_numeric_channel_id
from ..downloader import MediaDownloader


class QQSender:
    """
    负责将消息转发到 QQ 群
    """

    def __init__(
        self, context: star.Context, config: AstrBotConfig, downloader: MediaDownloader
    ):
        self.context = context
        self.config = config
        self.downloader = downloader
        self._group_locks = {}  # 群锁，防止并发发送
        self.platform_id = None  # 动态捕获的平台 ID
        self.bot = None  # 动态捕获的 bot 实例
        self.node_name = None  # 合并转发消息时显示的 bot 昵称
        self._cached_self_id = None  # 缓存 bot 的 user_id

    async def _ensure_node_name(self, bot, cache_fallback: bool = False) -> tuple[str, int]:
        """获取 bot 昵称和 user_id，返回 (node_name, self_id)"""
        if self.node_name and self.node_name != "AstrBot":
            return (self.node_name, self._cached_self_id or 0)

        try:
            info = await bot.get_login_info()
            if info:
                if nickname := info.get("nickname"):
                    self.node_name = str(nickname)
                    logger.debug(f"[QQSender] 获取到 bot 昵称: {self.node_name}")
                self._cached_self_id = info.get("user_id", 0)
                logger.debug(f"[QQSender] 获取到 bot user_id: {self._cached_self_id}")
            else:
                logger.debug(f"[QQSender] 未能从登录信息获取到昵称")
        except Exception as e:
            logger.debug(f"[QQSender] 获取 bot 信息异常: {e}")

        if cache_fallback and not self.node_name:
            self.node_name = "AstrBot"
        return (self.node_name, self._cached_self_id or 0)

    def _get_lock(self, group_id):
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _map_path(self, fpath: str) -> str:
        """映射文件路径（用于跨 Docker 容器文件访问）"""
        try:
            if path_Mapping is None:
                return fpath
            core_config = getattr(self.context, "_config", None)
            if core_config:
                mappings = core_config.get("platform_settings", {}).get(
                    "path_mapping", []
                )
                if mappings:
                    return path_Mapping(mappings, fpath)
        except Exception:
            pass
        return fpath

    async def initialize_runtime(self):
        """Best-effort bootstrap for platform_id/bot before first forward."""
        await self._bootstrap_qq_runtime()
        if not self.platform_id:
            logger.warning(
                "[QQSender] 初始化阶段未捕获到 QQ 平台实例，后续若使用纯数字目标将无法自动拼接会话名。"
            )

    def _get_platform_instances(self) -> list:
        pm = getattr(self.context, "platform_manager", None)
        if not pm:
            return []
        if hasattr(pm, "platform_insts"):
            insts = getattr(pm, "platform_insts") or []
            return list(insts)
        if hasattr(pm, "get_insts"):
            try:
                insts = pm.get_insts()
                return list(insts or [])
            except Exception:
                return []
        return []

    @staticmethod
    def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
        seen = set()
        result: List[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _split_qq_targets(targets: list) -> tuple[List[str], List[str]]:
        """Split config targets into full sessions and numeric group IDs."""
        session_targets: List[str] = []
        group_ids: List[str] = []
        for raw in targets:
            if raw is None:
                continue
            if isinstance(raw, str):
                val = raw.strip()
            else:
                val = str(raw).strip()
            if not val:
                continue
            if ":" in val:
                session_targets.append(val)
            elif val.isdigit():
                group_ids.append(val)
            else:
                logger.warning(f"[QQSender] Ignore invalid QQ target: {val}")
        return session_targets, group_ids

    @staticmethod
    def _session_platform_ids(session_targets: List[str]) -> List[str]:
        platform_ids: List[str] = []
        for session in session_targets:
            if ":" not in session:
                continue
            platform_ids.append(session.split(":", 1)[0])
        return QQSender._dedupe_keep_order(platform_ids)

    async def _bootstrap_qq_runtime(
        self, preferred_platform_ids: Optional[List[str]] = None
    ):
        """Try to fetch platform_id and bot from context.platform_manager."""
        if self.platform_id and self.bot:
            return

        platforms = self._get_platform_instances()
        if not platforms:
            return

        candidates = []
        for platform in platforms:
            try:
                meta = platform.meta()
                pid = getattr(meta, "id", None)
                pname_raw = str(getattr(meta, "name", ""))
                pname = pname_raw.lower()
                if pid:
                    candidates.append((platform, str(pid), pname, pname_raw))
            except Exception:
                continue

        if not candidates:
            return

        selected = None
        preferred = set(preferred_platform_ids or [])
        if preferred:
            for item in candidates:
                if item[1] in preferred:
                    selected = item
                    break
        if selected is None and self.platform_id:
            for item in candidates:
                if item[1] == self.platform_id:
                    selected = item
                    break
        if selected is None:
            for item in candidates:
                if item[2] == "aiocqhttp":
                    selected = item
                    break
        if selected is None:
            for item in candidates:
                if "qq" in item[2] or "onebot" in item[2]:
                    selected = item
                    break
        if selected is None:
            selected = candidates[0]

        platform, pid, _, pname_raw = selected
        self.platform_id = pid
        logger.debug(
            f"[QQSender] 捕获到 QQ 平台: platform_id={pid}, platform_name={pname_raw or 'unknown'}"
        )

        try:
            if hasattr(platform, "get_client"):
                self.bot = platform.get_client()
            elif hasattr(platform, "bot"):
                self.bot = platform.bot
        except Exception as e:
            logger.debug(f"[QQSender] Bootstrap bot from platform failed: {e}")

        if self.bot:
            await self._ensure_node_name(self.bot, cache_fallback=False)

    async def send(
        self,
        batches: List[List[Message]],
        src_channel: str,
        display_name: str = None,
        effective_cfg: dict = None,
        involved_channels: List[str] = None,  # 新增：混合模式时传入实际涉及的频道列表
    ):
        """
        转发消息到 QQ 群
        """
        if effective_cfg is None:
            effective_cfg = {}

        if involved_channels and len(involved_channels) > 1:
            global_cfg = self.config.get("forward_config", {})
            strip_links = global_cfg.get("strip_markdown_links", False)
            exclude_text_on_media = global_cfg.get("exclude_text_on_media", False)
        else:
            exclude_text_on_media = effective_cfg.get("exclude_text_on_media", False)
            strip_links = effective_cfg.get("strip_markdown_links", False)

        channel_specific_targets = effective_cfg.get("effective_target_qq_sessions", [])
        if channel_specific_targets:  # 非空列表 → 使用频道专属配置
            effective_qq_targets = channel_specific_targets
        else:
            effective_qq_targets = self.config.get("target_qq_session", [])

        qq_targets = effective_qq_targets

        if not qq_targets or not batches:
            return

        if isinstance(qq_targets, int):
            qq_targets = [qq_targets]
        elif not isinstance(qq_targets, list):
            return

        session_targets_cfg, numeric_group_ids = self._split_qq_targets(qq_targets)
        preferred_platform_ids = self._session_platform_ids(session_targets_cfg)
        if numeric_group_ids or session_targets_cfg:
            await self._bootstrap_qq_runtime(
                preferred_platform_ids=preferred_platform_ids
            )

        context_target_sessions = list(session_targets_cfg)
        qq_platform_id = self.platform_id
        if numeric_group_ids:
            if qq_platform_id:
                context_target_sessions.extend(
                    [
                        f"{qq_platform_id}:GroupMessage:{gid}"
                        for gid in numeric_group_ids
                    ]
                )
            else:
                logger.warning(
                    "[QQSender] Localhost mode cannot resolve platform_id for numeric QQ target. "
                    "Use full session name (platform:MessageType:target_id) or ensure platform is loaded."
                )
        context_target_sessions = self._dedupe_keep_order(context_target_sessions)

        if not context_target_sessions:
            return

        forward_cfg = self.config.get("forward_config", {})
        qq_merge_threshold = forward_cfg.get("qq_merge_threshold", 0)

        # 兼容大合并调用时多包一层的情况
        real_batches = []
        for item in batches:
            if (
                isinstance(item, list)
                and item
                and all(isinstance(sub, list) for sub in item)
            ):
                real_batches.extend(item)
            else:
                real_batches.append(item)

        if not real_batches:
            logger.debug("[QQSender] 展平后无有效批次，跳过发送")
            return

        logger.debug(
            f"[QQSender] 接收到 {len(batches)} 批次，展平后 {len(real_batches)} 个逻辑批次"
        )

        if context_target_sessions:
            bot = self.bot
            if not bot and qq_platform_id:
                try:
                    platform = self.context.get_platform_inst(qq_platform_id)
                    if platform:
                        if hasattr(platform, "get_client"):
                            bot = platform.get_client()
                        elif hasattr(platform, "bot"):
                            bot = platform.bot
                except Exception as e:
                    logger.error(f"[QQSender] Failed to get bot instance: {e}")
            if bot and not self.bot:
                self.bot = bot

            node_name, self_id = (
                await self._ensure_node_name(bot, cache_fallback=True)
                if bot
                else ("AstrBot", 0)
            )

            # ─── 判断是否为混合频道大合并模式 ───
            is_mixed_big_merge = bool(involved_channels and len(involved_channels) > 1)

            if is_mixed_big_merge:
                # 构造清晰的多频道 From
                formatted = [
                    ch if is_numeric_channel_id(ch) else f"@{ch.lstrip('@')}"
                    for ch in sorted(involved_channels)
                ]
                if len(formatted) <= 4:
                    channels_str = " ".join(formatted)
                else:
                    channels_str = (
                        " ".join(formatted[:4]) + f" 等{len(formatted) - 4}个频道"
                    )
                header = f"From {channels_str}:"
                logger.debug(f"[QQSender] 混合大合并 From: {header}")
            else:
                # 普通情况（单频道或独立频道模式）
                header_name = display_name or src_channel
                if not header_name.startswith("@") and not is_numeric_channel_id(
                    header_name.lstrip("@")
                ):
                    header_name = f"@{header_name}"
                header = f"From {header_name}:"

            # 预处理所有批次
            processed_batches = []
            header_added = False  # 用于混合模式：只在全局第一个节点加 header

            for msgs in real_batches:
                all_local_files = []
                all_nodes_data = []
                try:
                    for i, msg in enumerate(msgs):
                        current_node_components = []
                        text_parts = []
                        if msg.text:
                            cleaned = clean_telegram_text(
                                msg.text, strip_links=strip_links
                            )
                            if cleaned:
                                text_parts.append(cleaned)

                        media_components = []
                        has_any_attachment = False
                        msg_max_size = getattr(msg, "_max_file_size", 0)
                        files = await self.downloader.download_media(
                            msg, max_size_mb=msg_max_size
                        )
                        for fpath in files:
                            all_local_files.append(fpath)
                            has_any_attachment = True
                            ext = os.path.splitext(fpath)[1].lower()
                            if ext in [
                                ".jpg",
                                ".jpeg",
                                ".png",
                                ".webp",
                                ".gif",
                                ".bmp",
                            ]:
                                media_components.append(Image.fromFileSystem(fpath))
                            elif ext == ".wav":
                                media_components.append(Record.fromFileSystem(fpath))
                            elif ext == ".mp4":
                                # Video 在合并转发中由 NapCat 直接读取文件，
                                # 跨 Docker 环境需要映射为容器可访问的路径
                                mapped = self._map_path(fpath)
                                if mapped != fpath:
                                    media_components.append(
                                        Video(file=f"file:///{mapped}")
                                    )
                                else:
                                    media_components.append(Video.fromFileSystem(fpath))
                            else:
                                media_components.append(
                                    File(file=fpath, name=os.path.basename(fpath))
                                )

                        should_exclude_text = (
                            exclude_text_on_media and has_any_attachment
                        )

                        # ─── 决定是否添加 From 头部 ───
                        add_header_this_time = False
                        # 媒体消息仅发送媒体模式下，不添加 From 头部
                        if not should_exclude_text:
                            if is_mixed_big_merge:
                                # 混合大合并：**只在整个合并的第一个消息**加 From
                                if not header_added and i == 0:
                                    add_header_this_time = True
                                    header_added = True
                            else:
                                # 普通/独立模式：每个小相册/单条的第一个消息加 From
                                if i == 0:
                                    add_header_this_time = True

                        if add_header_this_time:
                            if text_parts:
                                text_parts[0] = f"{header}\n\u200b{text_parts[0]}"
                            else:
                                current_node_components.append(
                                    Plain(f"{header}\n\u200b")
                                )

                        if not should_exclude_text:
                            for t in text_parts:
                                current_node_components.append(Plain(t + "\n"))

                        # 文字 + 特殊媒体(Video/Record/File)时拆分为独立 Node，
                        # 与单条消息路径的 special_types 拆分逻辑对齐 (issue#16)
                        # 注意：Image 不拆分（原设计：From header 在外层显示，内层只渲染图片）
                        _node_special_media = [
                            c
                            for c in media_components
                            if isinstance(c, (Video, Record, File))
                        ]
                        if (
                            not should_exclude_text
                            and text_parts
                            and _node_special_media
                        ):
                            if current_node_components:
                                all_nodes_data.append(current_node_components)
                            current_node_components = []

                        current_node_components.extend(media_components)

                        if current_node_components:
                            # 避免生成只有 header 的空节点
                            is_only_header = (
                                len(current_node_components) == 1
                                and isinstance(current_node_components[0], Plain)
                                and current_node_components[0].text.strip("\u200b\n")
                                in [header, ""]
                            )
                            if not is_only_header:
                                all_nodes_data.append(current_node_components)

                    if all_nodes_data:
                        processed_batches.append(
                            {
                                "nodes_data": all_nodes_data,
                                "local_files": all_local_files,
                            }
                        )
                except Exception as e:
                    logger.error(f"[QQSender] 预处理消息批次异常: {e}")
                    self._cleanup_files(all_local_files)

            use_big_merge = (qq_merge_threshold > 1) and (
                len(processed_batches) >= qq_merge_threshold
            )
            if use_big_merge and not is_mixed_big_merge:
                logger.info(
                    f"[QQSender] 本次 {len(processed_batches)} 个逻辑单元 >= 阈值 {qq_merge_threshold}，转为整组合并转发"
                )

            # 发送到各个目标群组
            for target_session in context_target_sessions:
                if not target_session:
                    continue
                lock = self._get_lock(target_session)
                async with lock:
                    unified_msg_origin = target_session

                    if use_big_merge or is_mixed_big_merge:
                        # ─── 大合并（包括混合模式） ───
                        all_sub_nodes_data = []
                        for batch_data in processed_batches:
                            all_sub_nodes_data.extend(batch_data["nodes_data"])

                        if all_sub_nodes_data:
                            try:
                                nodes_list = [
                                    Node(uin=self_id, name=node_name, content=nc)
                                    for nc in all_sub_nodes_data
                                ]
                                message_chain = MessageChain()
                                message_chain.chain.append(Nodes(nodes_list))
                                await self.context.send_message(
                                    unified_msg_origin, message_chain
                                )
                                logger.info(
                                    f"[QQSender] {node_name} -> {target_session}: "
                                    f"{'混合' if is_mixed_big_merge else ''}大合并转发 "
                                    f"({len(all_sub_nodes_data)} 子节点 / {len(processed_batches)} 逻辑单元)"
                                )
                            except Exception as e:
                                logger.error(
                                    f"[QQSender] 大合并转发到 {target_session} 失败: {e}"
                                )
                    else:
                        # 普通发送（逐个小相册 / 单条）
                        for batch_data in processed_batches:
                            all_nodes_data = batch_data["nodes_data"]
                            try:
                                if len(all_nodes_data) > 1:
                                    # 小范围相册合并
                                    message_chain = MessageChain()
                                    nodes_list = [
                                        Node(uin=self_id, name=node_name, content=nc)
                                        for nc in all_nodes_data
                                    ]
                                    message_chain.chain.append(Nodes(nodes_list))
                                    await self.context.send_message(
                                        unified_msg_origin, message_chain
                                    )
                                    logger.info(
                                        f"[QQSender] {node_name} -> {target_session}: 相册合并 ({len(all_nodes_data)} 节点)"
                                    )
                                else:
                                    # 单条消息（可能含特殊媒体需拆分）
                                    components = all_nodes_data[0]
                                    special_types = (Record, File, Video)
                                    has_special = any(
                                        isinstance(c, special_types) for c in components
                                    )
                                    if has_special:
                                        for c in components:
                                            if isinstance(c, special_types):
                                                chain = MessageChain([c])
                                                await self.context.send_message(
                                                    unified_msg_origin, chain
                                                )
                                        common_components = [
                                            c
                                            for c in components
                                            if not isinstance(c, special_types)
                                        ]
                                        if common_components:
                                            chain = MessageChain()
                                            chain.chain.extend(common_components)
                                            await self.context.send_message(
                                                unified_msg_origin, chain
                                            )
                                        logger.info(
                                            f"[QQSender] {node_name} -> {target_session}: 单条消息 (已拆分特殊媒体)"
                                        )
                                    else:
                                        message_chain = MessageChain()
                                        message_chain.chain.extend(components)
                                        await self.context.send_message(
                                            unified_msg_origin, message_chain
                                        )
                                        logger.info(
                                            f"[QQSender] {node_name} -> {target_session}: 单条普通消息"
                                        )
                                await asyncio.sleep(1)
                            except Exception as e:
                                logger.error(
                                    f"[QQSender] 转发到 {target_session} 异常: {e}"
                                )

            # 清理文件
            for batch_data in processed_batches:
                self._cleanup_files(batch_data["local_files"])

    def _cleanup_files(self, files: List[str]):
        """清理临时下载的文件"""
        for f in files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass
