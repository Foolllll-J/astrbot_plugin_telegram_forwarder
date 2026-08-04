from telethon import TelegramClient
import sys
import gc
import socks
from astrbot.api import logger, AstrBotConfig
import asyncio
import os
from urllib.parse import urlparse

# ========== 全局客户端缓存 ==========
# 避免插件重载时重新连接和授权，大幅提升配置保存速度
# 使用 sys 模块确保缓存跨插件重载持久化
def get_client_cache():
    if not hasattr(sys, "_telegram_forwarder_client_cache"):
        sys._telegram_forwarder_client_cache = {}
    return sys._telegram_forwarder_client_cache


def get_auth_cache():
    """获取授权状态缓存"""
    if not hasattr(sys, "_telegram_forwarder_auth_cache"):
        sys._telegram_forwarder_auth_cache = {}
    return sys._telegram_forwarder_auth_cache


class TelegramClientWrapper:
    """
    Telegram 客户端封装类

    负责创建和管理 Telethon 客户端实例。
    """

    DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0

    def __init__(self, config: AstrBotConfig, plugin_data_dir: str):
        """
        初始化客户端封装

        Args:
            config: AstrBot 配置对象，包含 api_id、api_hash、代理等
            plugin_data_dir: 插件数据目录，用于存储会话文件
        """
        self.config = config
        self.plugin_data_dir = plugin_data_dir
        self.client = None
        self._authorized = False
        self.connect_timeout_seconds = self._load_connect_timeout_seconds()
        self._init_client()

    def _session_path(self) -> str:
        return os.path.join(self.plugin_data_dir, "user_session")

    def _load_connect_timeout_seconds(self) -> float:
        raw_timeout = self.config.get(
            "connect_timeout_seconds",
            self.DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = self.DEFAULT_CONNECT_TIMEOUT_SECONDS
        return max(timeout, 0.1)

    async def _connect_with_timeout(self) -> bool:
        if not self.client:
            return False
        try:
            await asyncio.wait_for(
                self.client.connect(),
                timeout=self.connect_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Client] Telegram connect timed out after "
                f"{self.connect_timeout_seconds:.1f}s"
            )
            return False
        except Exception as e:
            logger.error(f"[Client] Telegram connect failed: {e}")
            return False
        return self.client.is_connected()

    async def ensure_connected(self) -> bool:
        if not self.client:
            return False
        if self.client.is_connected():
            return True
        return await self._connect_with_timeout()

    async def disconnect(self, timeout: float = 5.0) -> None:
        """Safely disconnect the current Telethon client."""
        if not self.client:
            return

        try:
            # 先断开网络连接
            if self.client.is_connected():
                await asyncio.wait_for(self.client.disconnect(), timeout=timeout)

            # 强制关闭 SQLite session，释放文件锁
            if hasattr(self.client, 'session') and hasattr(self.client.session, 'close'):
                self.client.session.close()
                logger.debug("[Client] 已关闭 SQLite session 连接")
        except asyncio.TimeoutError:
            logger.warning(f"[Client] disconnect 超时 ({timeout}s)")
        except Exception as e:
            logger.debug(f"[Client] disconnect 异常: {e}")

    async def send_login_code(self, phone: str) -> str:
        """发送登录验证码并返回 phone_code_hash。"""
        if not await self.ensure_connected():
            raise RuntimeError("Telegram 客户端未初始化，请先设置 api_id/api_hash")
        sent = await asyncio.wait_for(self.client.send_code_request(phone), timeout=30.0)
        return getattr(sent, "phone_code_hash", "")

    async def sign_in_with_code(self, phone: str, code: str, phone_code_hash: str = ""):
        """Use login code to sign in. Returns (ok, False); 2FA is signaled via SessionPasswordNeededError."""
        if not await self.ensure_connected():
            raise RuntimeError("Telegram 客户端未初始化，请先设置 api_id/api_hash")
        if phone_code_hash:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        else:
            await self.client.sign_in(phone=phone, code=code)
        return await self._mark_authorized_if_needed()

    async def sign_in_with_password(self, password: str) -> bool:
        """提交两步验证密码。"""
        if not await self.ensure_connected():
            raise RuntimeError("Telegram 客户端未初始化，请先设置 api_id/api_hash")
        await self.client.sign_in(password=password)
        ok, _ = await self._mark_authorized_if_needed()
        return ok

    async def _mark_authorized_if_needed(self):
        authorized = await self.client.is_user_authorized()
        if authorized:
            self._authorized = True
            auth_cache = get_auth_cache()
            auth_cache[self._session_path()] = True
            # 某些会话（例如 bot 会话）可能无权限调用 get_dialogs，
            # 此时不应影响”已授权”状态判定。
            # 注意：只同步少量对话，避免大账号内存暴涨
            try:
                await asyncio.wait_for(
                    self.client.get_dialogs(limit=10),
                    timeout=30.0,
                )
            except Exception as e:
                logger.debug(f"[Client] skip get_dialogs after auth: {e}")
            return True, False
        return False, False

    def _init_client(self):
        """
        初始化 Telethon 客户端实例

        执行流程：
        1. 从配置读取 api_id 和 api_hash
        2. 设置会话文件路径
        3. 检查缓存中是否存在可用客户端
        4. 如不存在，解析代理配置并创建新客户端
        5. 将新客户端加入缓存

        代理支持：
            - HTTP 代理：http://host:port
            - SOCKS5 代理：socks5://host:port
            - 带认证的代理：socks5://user:pass@host:port

        Note:
            如果缺少 api_id/api_hash，client 将保持为 None
            使用全局缓存避免插件重载时重新连接，提升配置保存速度
        """
        # ========== 读取必要的 API 凭证 ==========
        api_id = self.config.get("api_id")
        api_hash = self.config.get("api_hash")

        # 只有在配置完整时才创建客户端
        if api_id and api_hash:
            # 会话文件路径：存储登录状态和缓存
            # 使用 .session 扩展名，Telethon 会自动添加
            session_path = self._session_path()

            # ========== 检查缓存 ==========
            cache = get_client_cache()

            # 尝试从缓存中获取已连接的客户端
            if session_path in cache:
                cached_client = cache[session_path]
                if cached_client and cached_client.is_connected():
                    logger.debug(f"[Client Cache] 复用现有的 Telegram 客户端连接: {session_path}")
                    self.client = cached_client
                    return
                else:
                    logger.debug(f"[Client Cache] 缓存的客户端已断开，正在重新创建: {session_path}")
                    # 强制关闭旧客户端的 SQLite session，释放文件锁
                    if cached_client and hasattr(cached_client, 'session') and hasattr(cached_client.session, 'close'):
                        try:
                            cached_client.session.close()
                            logger.debug(f"[Client Cache] 已关闭旧客户端的 SQLite session: {session_path}")
                        except Exception as e:
                            logger.debug(f"[Client Cache] 关闭 SQLite session 失败: {e}")
                    del cache[session_path]

                    # 等待 SQLite 文件锁完全释放 (防止 "database is locked")
                    import time
                    time.sleep(0.5)
                    logger.debug(f"[Client Cache] 已等待 SQLite 文件锁释放")

            # ========== 代理配置解析 ==========
            proxy_url = self.config.get("proxy", "")
            proxy_setting = None

            if proxy_url:
                try:
                    parsed = urlparse(proxy_url)
                    proxy_type = (
                        socks.HTTP if parsed.scheme.startswith("http") else socks.SOCKS5
                    )
                    proxy_setting = (proxy_type, parsed.hostname, parsed.port)
                    logger.debug(f"[Client] 使用代理: {proxy_url}")
                except (ValueError, AttributeError) as e:
                    logger.error(f"[Client] 代理 URL 格式错误: {e}")

            # ========== 创建 Telegram 客户端 ==========
            self.client = TelegramClient(
                session_path,
                api_id,
                api_hash,
                proxy=proxy_setting,
                connection_retries=0,
                retry_delay=5,
                auto_reconnect=True,
            )

            # ========== 加入缓存 ==========
            cache[session_path] = self.client
            logger.debug(f"[Client Cache] 已创建并缓存新的 Telegram 客户端: {session_path}")

        else:
            logger.warning(
                "Telegram Forwarder: 缺少 api_id 或 api_hash，请在配置中填写。"
            )

    async def start(self) -> bool:
        """
        启动 Telegram 客户端

        执行流程：
        1. 检查客户端是否已经连接并授权（从缓存复用）
        2. 如果已连接，跳过初始化直接返回
        3. 否则，连接到 Telegram 服务器
        4. 检查授权状态
        5. 如未授权，尝试登录（发送验证码）
        6. 同步对话框列表，确保能解析频道ID

        异常处理：
            - 网络超时：30秒后放弃
            - 未授权：输出错误提示，引导用户手动登录
            - 其他错误：记录日志并返回

        Note:
            在非交互式环境中无法完成验证码输入
            用户需要在交互式终端手动登录一次，生成会话文件
        """
        # 客户端未初始化时直接返回
        if not self.client:
            return False

        try:
            # ========== 快速路径：检查是否已连接并授权 ==========
            # 如果客户端是从缓存复用的，且已经连接并授权，直接返回
            if self.client.is_connected():
                session_path = self._session_path()
                auth_cache = get_auth_cache()

                if auth_cache.get(session_path, False):
                    self._authorized = True
                    logger.debug(f"[Client Cache] 复用已授权的客户端: {session_path}")
                    return True
                else:
                    authorized = await self.client.is_user_authorized()
                    if authorized:
                        auth_cache[session_path] = True
                        self._authorized = True
                        logger.debug(f"[Client Cache] 复用已授权的客户端: {session_path}")
                        return True

            # ========== 慢速路径：完整初始化 ==========
            if not await self._connect_with_timeout():
                self._authorized = False
                return False

            # ========== 检查授权状态 ==========
            authorized = await self.client.is_user_authorized()
            if not authorized:
                logger.warning(f"[Client] 客户端未授权。会话路径: {os.path.join(self.plugin_data_dir, 'user_session.session')}")

                phone = self.config.get("phone")
                if phone:
                    logger.info(f"[Client] 正在尝试使用手机号 {phone} 登录...")
                    try:
                        await asyncio.wait_for(
                            self.client.send_code_request(phone), timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        logger.error("[Client] 发送验证码请求超时")
                        return False

                    logger.error("[Client] Telegram 客户端需要验证！请在交互式终端运行一次以完成登录。")
                    return False
                else:
                    logger.error("[Client] 未提供手机号，无法登录。")
                    return False

            # ========== 授权成功 ==========
            logger.info("[Client] Telegram 客户端授权成功！")
            self._authorized = True

            session_path = self._session_path()
            auth_cache = get_auth_cache()
            auth_cache[session_path] = True

            # ========== 同步对话框 ==========
            # 注意：只同步少量对话，避免大账号内存暴涨（limit=None 会加载所有对话，可能数百MB）
            logger.debug("[Client] 正在同步对话框...")
            try:
                await asyncio.wait_for(self.client.get_dialogs(limit=10), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("[Client] 对话框同步超时，可能影响频道解析，但尝试继续。")
            logger.debug("[Client] 对话框同步完成")
            return True

        except Exception as e:
            logger.error(f"[Client] Telegram 客户端错误: {e}")
            self._authorized = False
            return False

    def is_connected(self):
        """检查客户端连接状态"""
        return self.client and self.client.is_connected()

    def is_authorized(self):
        """检查客户端是否已授权"""
        return getattr(self, "_authorized", False) and self.is_connected()

    @staticmethod
    def clear_cache(session_path=None):
        """清理客户端缓存和授权状态缓存"""
        cache = get_client_cache()
        auth_cache = get_auth_cache()

        if session_path:
            if session_path in cache:
                logger.debug(f"[Client Cache] 清理会话缓存: {session_path}")
                # 强制关闭 SQLite session，释放文件锁
                cached_client = cache[session_path]
                if cached_client and hasattr(cached_client, 'session') and hasattr(cached_client.session, 'close'):
                    try:
                        cached_client.session.close()
                        logger.debug(f"[Client Cache] 清理缓存时已关闭 SQLite session: {session_path}")
                    except Exception as e:
                        logger.debug(f"[Client Cache] 清理缓存时关闭 SQLite session 失败: {e}")
                del cache[session_path]
            if session_path in auth_cache:
                del auth_cache[session_path]
        else:
            client_count = len(cache)
            logger.debug(f"[Client Cache] 清理所有缓存 ({client_count} 个会话)")
            # 关闭所有 SQLite session
            for path, client in list(cache.items()):
                if client and hasattr(client, 'session') and hasattr(client.session, 'close'):
                    try:
                        client.session.close()
                    except Exception:
                        pass
            cache.clear()
            auth_cache.clear()

    @staticmethod
    def cleanup_disconnected_cache():
        """清理全局缓存中所有已断开的客户端，防止内存泄漏"""
        cache = get_client_cache()
        auth_cache = get_auth_cache()
        disconnected_sessions = []

        for session_path, client in list(cache.items()):
            if client is None or not client.is_connected():
                disconnected_sessions.append(session_path)

        for session_path in disconnected_sessions:
            del cache[session_path]
            if session_path in auth_cache:
                del auth_cache[session_path]

        if disconnected_sessions:
            logger.debug(
                f"[Client Cache] 清理了 {len(disconnected_sessions)} 个断开的缓存客户端"
            )

    async def force_reconnect(self) -> bool:
        """
        强制断开当前客户端，清理缓存，并创建全新的连接。

        处理场景：Telegram 连接假死（zombie connection），此时 is_connected()
        仍返回 True 但实际请求无法完成，导致超时循环。

        流程:
        1. 断开旧客户端（可能已假死）
        2. 清理所有缓存（sys 全局缓存 + 授权缓存）
        3. 重置状态，创建新的 TelegramClient 实例
        4. 连接并验证授权
        5. **不**同步完整对话列表（避免大账号内存暴涨）

        Returns:
            True 表示重连成功，False 表示失败
        """
        session_path = self._session_path()
        logger.warning(
            f"[Client] 正在强制重连 Telegram 客户端 (session: {session_path})..."
        )

        # 1. 断开旧客户端（忽略超时/异常，尽力断开）
        old_client = self.client
        if old_client:
            try:
                await asyncio.wait_for(
                    old_client.disconnect(), timeout=15.0
                )
                logger.debug("[Client] 旧客户端已断开")
            except asyncio.TimeoutError:
                logger.warning("[Client] 断开旧客户端超时，强制清理。")
            except Exception as e:
                logger.debug(f"[Client] 断开旧客户端异常 (可忽略): {e}")
            finally:
                # 确保旧客户端的 SQLite session 关闭，释放文件锁及内存
                try:
                    if hasattr(old_client, 'session') and old_client.session:
                        old_client.session.close()
                except Exception:
                    pass
                del old_client

        # 2. 清理所有缓存 (sys 模块全局缓存)
        TelegramClientWrapper.clear_cache(session_path)

        # 3. 重置 wrapper 状态，断开引用链
        self.client = None
        self._authorized = False

        # 4. 主动触发 GC，回收旧 client 及内部循环引用
        gc.collect()

        # 5. 重新初始化（创建全新的 TelegramClient）
        self._init_client()

        if not self.client:
            logger.error("[Client] 强制重连: 创建新客户端失败。")
            return False

        # 6. 连接新客户端
        if not await self._connect_with_timeout():
            logger.error("[Client] 强制重连: 新客户端连接失败。")
            return False

        # 7. 验证授权
        try:
            authorized = await self.client.is_user_authorized()
            if not authorized:
                logger.error(
                    "[Client] 强制重连: 客户端未授权，需要重新登录。"
                )
                self._authorized = False
                return False

            self._authorized = True
            auth_cache = get_auth_cache()
            auth_cache[session_path] = True

            # ⚠️ 关键修复：不同步完整对话框列表
            # 原因：limit=None 会加载所有群组/频道/私聊，大账号可能数百MB甚至1GB+
            # 代理不稳定时频繁重连会导致内存在几分钟内爆炸式增长
            # Telethon 会在实际使用时按需解析实体，不需要预加载
            logger.debug("[Client] 强制重连成功，跳过对话框同步（按需解析实体）")

            logger.info("[Client] 强制重连成功！已创建新客户端连接。")
            return True
        except Exception as e:
            logger.error(f"[Client] 强制重连后授权检查失败: {e}")
            self._authorized = False
            return False

    @staticmethod
    async def disconnect_and_clear_cache(
        session_path: str, timeout: float = 5.0
    ) -> None:
        """Disconnect any cached client for a session and then clear caches."""
        cache = get_client_cache()
        cached_client = cache.get(session_path)

        try:
            if cached_client:
                # 断开网络连接
                if cached_client.is_connected():
                    await asyncio.wait_for(cached_client.disconnect(), timeout=timeout)

                # 强制关闭 SQLite session，释放文件锁
                if hasattr(cached_client, 'session') and hasattr(cached_client.session, 'close'):
                    cached_client.session.close()
                    logger.debug(f"[Client Cache] 已关闭 SQLite session 连接: {session_path}")
        except asyncio.TimeoutError:
            logger.warning(f"[Client Cache] 断开缓存客户端超时: {session_path}")
        except Exception as e:
            logger.debug(f"[Client Cache] 断开缓存客户端失败 {session_path}: {e}")
        finally:
            TelegramClientWrapper.clear_cache(session_path)
