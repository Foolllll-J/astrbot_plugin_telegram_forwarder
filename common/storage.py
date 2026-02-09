import json
import os
from astrbot.api import logger


class Storage:
    """
    数据持久化管理类
    """

    def __init__(self, data_file: str):
        """
        初始化存储管理器
        """
        self.data_file = data_file
        self.persistence = self._load()

    def _load(self) -> dict:
        """从文件加载持久化数据"""
        default_data = {"channels": {}}

        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"[Storage] 无法加载数据文件: {e}，将使用默认配置")
                return default_data

        return default_data

    def save(self):
        """保存当前数据到文件"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.persistence, f, indent=2)
        except IOError as e:
            logger.error(f"[Storage] 保存数据失败: {e}")

    def get_channel_data(self, channel_name: str) -> dict:
        """获取频道的持久化数据"""
        if channel_name not in self.persistence["channels"]:
            self.persistence["channels"][channel_name] = {
                "last_post_id": 0,
                "channel_id": None, # 记录频道的数字 ID，用于转发查重
                "pending_queue": []
            }
        
        if "pending_queue" not in self.persistence["channels"][channel_name]:
            self.persistence["channels"][channel_name]["pending_queue"] = []
            
        if "channel_id" not in self.persistence["channels"][channel_name]:
            self.persistence["channels"][channel_name]["channel_id"] = None
            
        return self.persistence["channels"][channel_name]

    def update_channel_id(self, channel_name: str, channel_id: int):
        """更新频道的数字 ID"""
        data = self.get_channel_data(channel_name)
        if data.get("channel_id") != channel_id:
            data["channel_id"] = channel_id
            self.save()
            logger.debug(f"[Storage] 更新频道 {channel_name} 的数字 ID 为 {channel_id}")

    def get_channel_name_by_id(self, channel_id: int) -> str:
        """根据数字 ID 获取频道名"""
        for name, info in self.persistence.get("channels", {}).items():
            if info.get("channel_id") == channel_id:
                return name
        return None

    def add_to_pending_queue(self, channel_name: str, msg_id: int, timestamp: float, grouped_id: int = None, is_cold_start: bool = False):
        """添加单条消息到待发送队列"""
        data = self.get_channel_data(channel_name)
        if not any(m["id"] == msg_id for m in data["pending_queue"]):
            data["pending_queue"].append({
                "id": msg_id, 
                "time": timestamp,
                "grouped_id": grouped_id,
                "is_cold_start": is_cold_start
            })
            self.save()
            logger.debug(f"[Storage] 消息 {msg_id} (组: {grouped_id}) 已保存到 {channel_name} 待发送队列。当前队列大小: {len(data['pending_queue'])}")
        else:
            logger.debug(f"[Storage] 消息 {msg_id} 已在队列中，跳过。")

    def update_pending_queue(self, channel_name: str, queue: list):
        """更新频道的待发送队列"""
        data = self.get_channel_data(channel_name)
        old_len = len(data["pending_queue"])
        data["pending_queue"] = queue
        self.save()
        if old_len != len(queue):
            logger.debug(f"[Storage] 更新 {channel_name} 队列长度: {old_len} -> {len(queue)}")

    def get_all_pending(self) -> list:
        """获取所有频道的所有待发送消息"""
        all_pending = []
        for channel_name, info in self.persistence.get("channels", {}).items():
            for msg in info.get("pending_queue", []):
                all_pending.append({
                    "channel": channel_name,
                    "id": msg["id"],
                    "time": msg["time"],
                    "grouped_id": msg.get("grouped_id"),
                    "is_cold_start": msg.get("is_cold_start", False)
                })
        return all_pending

    def remove_ids_from_pending(self, channel_name: str, msg_ids: list):
        """从待发送队列中移除指定 ID 的消息"""
        data = self.get_channel_data(channel_name)
        original_len = len(data["pending_queue"])
        data["pending_queue"] = [m for m in data["pending_queue"] if m["id"] not in msg_ids]
        if len(data["pending_queue"]) != original_len:
            self.save()
            logger.debug(f"[Storage] 从 {channel_name} 队列移除了 {original_len - len(data['pending_queue'])} 条消息")

    def cleanup_expired_pending(self, retention_seconds: int):
        """清理所有频道中过期的消息"""
        import time
        now = time.time()
        cleaned_total = 0
        for channel_name, info in self.persistence.get("channels", {}).items():
            old_queue = info.get("pending_queue", [])
            # 冷启动消息不参与过期清理
            new_queue = [m for m in old_queue if m.get("is_cold_start", False) or (now - m["time"] <= retention_seconds)]
            if len(new_queue) != len(old_queue):
                cleaned_total += (len(old_queue) - len(new_queue))
                info["pending_queue"] = new_queue
        
        if cleaned_total > 0:
            self.save()
            logger.debug(f"[Storage] 全局清理了 {cleaned_total} 条过期消息。")
        return cleaned_total

    def reset_inactive_channels(self, active_channels: list):
        """
        将不在 active_channels 列表中的频道的 last_post_id 清零。
        这确保了如果将来重新启用这些频道，冷启动逻辑能正确触发。
        """
        changed = False
        for channel_name, info in self.persistence.get("channels", {}).items():
            if channel_name not in active_channels:
                if info.get("last_post_id", 0) != 0:
                    info["last_post_id"] = 0
                    logger.info(f"[Storage] 频道 {channel_name} 当前未被监控，已重置其 last_post_id 为 0")
                    changed = True
        
        if changed:
            self.save()
    def update_last_id(self, channel_name: str, last_id: int):
        """
        更新频道的最后处理消息ID

        Args:
            channel_name: 频道名称或ID
            last_id: 最后处理的消息ID

        行为：
            - 立即保存到文件，确保持久化
            - 如果频道不存在，自动创建
        """
        # 确保频道存在
        if channel_name not in self.persistence["channels"]:
            self.persistence["channels"][channel_name] = {}

        # 更新最后消息ID
        self.persistence["channels"][channel_name]["last_post_id"] = last_id

        # 立即保存到文件
        self.save()
