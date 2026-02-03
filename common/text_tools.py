import re


def clean_telegram_text(text: str) -> str:
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

    # 3. 去除 Markdown 格式标记
    text = text.replace("**", "").replace("__", "")

    # 4. 优化 Markdown 链接显示
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", text)

    return text.strip()
