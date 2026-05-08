import re


def normalize_telegram_channel_name(raw: str) -> str:
    """标准化频道用户名、t.me 链接或数字 ID。

    支持：
    - 用户名：@channel → channel
    - URL：https://t.me/channel → channel
    - 数字 ID：#-3791533773 → -3791533773, -3791533773 → -3791533773
    """
    text = (raw or "").strip()
    if not text:
        return ""

    # 处理 # 前缀的数字频道 ID，避免被后续 URL fragment 剥离逻辑误删
    # 例如 #-3791533773 → -3791533773
    if text.startswith("#") and len(text) > 1 and (text[1] == "-" or text[1].isdigit()):
        text = text[1:]

    text = text.lstrip("@").strip()
    lower = text.lower()
    if lower.startswith("https://t.me/") or lower.startswith("http://t.me/"):
        text = text.split("://", 1)[1]
    if text.lower().startswith("t.me/"):
        text = text.split("/", 1)[1]

    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    return text.lstrip("@").strip()


def is_numeric_channel_id(name: str) -> bool:
    """判断频道名是否为数字 ID（而非用户名）。"""
    return bool(name) and name.lstrip("-").isdigit()


def to_telethon_entity(channel_name: str) -> str | int:
    """将存储的频道名转换为 Telethon 可解析的实体标识符。

    Telethon 解析数字 ID 需要整数类型，用户名则需要字符串。
    此函数将数字型频道名字符串（如 '-3791533773'）转为 int，
    用户名保持字符串不变。

    Args:
        channel_name: 存储的频道标识符（用户名或数字 ID 字符串）

    Returns:
        数字 ID 返回 int，用户名返回 str
    """
    if not channel_name:
        return channel_name
    stripped = channel_name.lstrip("-")
    if stripped.isdigit():
        return int(channel_name)
    return channel_name


def clean_telegram_text(text: str, strip_links: bool = False) -> str:
    """清洗 Telegram 消息文本"""
    if not text:
        return ""

    # 1. 移除特定的频道签名
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if "频道" in line and "@" in line:
            continue
        if line.strip().startswith("@") and len(line) < 20:
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    # 2. 正则内容清洗
    patterns = [
        r"[\*＊\-]?\s*此原图经过处理.*",
        r"投稿 by .*",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # 3. 去除粗体/斜体标记（可选保留，根据需求）
    text = text.replace("**", "").replace("__", "")

    # 4. 处理 Markdown 链接  ← 这里是重点修改
    if strip_links:
        # 只保留 [文本] 部分，丢弃 (链接)
        text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    else:
        # 原有行为：显示 文本: 链接
        text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", text)

    return text.strip()
