import json
import os
from astrbot.api import logger


class Storage:
    """
    数据持久化管理类

    负责加载、保存和查询频道的消息ID状态信息。
    """

    def __init__(self, data_file: str):
        """
        初始化存储管理器

        Args:
            data_file: 数据文件路径，通常为 data.json

        行为：
            - 自动从文件加载已有数据
            - 如果文件不存在或损坏，使用默认数据
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
                "pending_queue": []
            }
        
        if "pending_queue" not in self.persistence["channels"][channel_name]:
            self.persistence["channels"][channel_name]["pending_queue"] = []
            
        return self.persistence["channels"][channel_name]

    def add_to_pending_queue(self, channel_name: str, msg_id: int, timestamp: float, grouped_id: int = None):
        """添加单条消息到待发送队列"""
        data = self.get_channel_data(channel_name)
        if not any(m["id"] == msg_id for m in data["pending_queue"]):
            data["pending_queue"].append({
                "id": msg_id, 
                "time": timestamp,
                "grouped_id": grouped_id
            })
            self.save()
            logger.debug(f"[Storage] 消息 {msg_id} (组: {grouped_id}) 已保存到 {channel_name} 待发送队列")

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
                    "grouped_id": msg.get("grouped_id")
                })
        return all_pending

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
