
import asyncio
import json
import re
import os
import httpx
from pathlib import Path
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger, star, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config # AstrBot injects the config based on _conf_schema.json
        
        # Setup Directories
        # We store persistence data in AstrBot/data/plugins/astrbot_plugin_telegram_forwarder
        self.plugin_data_dir = os.path.join(get_astrbot_data_path(), "plugins", "astrbot_plugin_telegram_forwarder")
        if not os.path.exists(self.plugin_data_dir):
            os.makedirs(self.plugin_data_dir)
            
        # Load Persistence Data (last_id)
        self.data_file = os.path.join(self.plugin_data_dir, "data.json")
        self.persistence = self._load_persistence()
        
        # Setup Scheduler
        self.scheduler = AsyncIOScheduler()
        
        if self.config.get("enabled", True):
            interval = self.config.get("check_interval", 60)
            self.scheduler.add_job(self.check_updates, 'interval', seconds=interval)
            self.scheduler.start()
            logger.info(f"Telegram Forwarder started. Watching: {self.config.get('source_channel')}")
        else:
            logger.warning("Telegram Forwarder is disabled in config.")

    def _load_persistence(self) -> dict:
        default_data = {"channels": {}}
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Simple migration check: if old format, ignore or reset
                    if "last_post_id" in data:
                        return default_data
                    return data
            except Exception:
                return default_data
        return default_data

    def _save_persistence(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.persistence, f, indent=2)

    async def check_updates(self):
        """Periodic task to check for new messages"""
        channels_config = self.config.get("source_channels", [])
        if not channels_config:
            return

        for cfg in channels_config:
            try:
                # Parse "Channel|2025-01-01"
                if "|" in cfg:
                    channel_name, date_str = cfg.split("|", 1)
                    channel_name = channel_name.strip()
                    start_date = None
                    try:
                        # Parse date string to datetime (assuming midnight UTC for simplicity, or naive)
                        start_date = datetime.strptime(date_str.strip(), "%Y-%m-%d")
                        # Make it timezone-aware (UTC) to match get_channel_messages if needed
                        # But scraping usually gives detailed offsets. Let's do a loose comparison inside.
                    except ValueError:
                        logger.error(f"Invalid date format in {cfg}. Use YYYY-MM-DD.")
                        start_date = None
                else:
                    channel_name = cfg.strip()
                    start_date = None
                
                await self._process_channel(channel_name, start_date)
            except Exception as e:
                logger.error(f"Error processing channel config {cfg}: {e}")
            # Slight delay between channels
            await asyncio.sleep(1)

    async def _process_channel(self, channel: str, start_date: Optional[datetime] = None):
        # Init channel persistence if needed
        if channel not in self.persistence["channels"]:
            self.persistence["channels"][channel] = {"last_post_id": 0, "forwarded_ids": []}
            
        channel_data = self.persistence["channels"][channel]
        last_id = channel_data.get("last_post_id", 0)
        forwarded = set(channel_data.get("forwarded_ids", []))

        try:
            messages = await self.get_channel_messages(channel, self.config.get("proxy"))
            if not messages:
                return

            # Feature: Cold Start / First Run
            if last_id == 0:
                if start_date:
                    # Find first message >= start_date (loose comparison)
                    target_id = 0
                    found_target = False
                    
                    # Sort by ID (time)
                    for m in messages:
                        if not m["date"]: continue
                        # Compare: Remove tzinfo for simpler comparison if needed, or handle offsets
                        # scrape gives offset-aware. start_date is naive (from strptime).
                        # Let's make start_date offset-aware (assume UTC or local? Telegram web usually UTC)
                        # Quick fix: make m["date"] naive or start_date aware.
                        msg_dt_naive = m["date"].replace(tzinfo=None)
                        
                        if msg_dt_naive >= start_date:
                            # Found the first message on/after the date
                            # We want to forward THIS message and all after it.
                            # So we set last_id to the one BEFORE it.
                            target_id = m["id"] - 1
                            found_target = True
                            logger.info(f"Found start date {start_date} match at msg {m['id']}. Starting from there.")
                            break
                    
                    if found_target:
                        self.persistence["channels"][channel]["last_post_id"] = target_id
                        self._save_persistence()
                        return # Return to allow next loop to pick it up normally? 
                        # Actually if we assume next loop runs soon, we can just return.
                        # But wait, if we return, we wait 60s. Better to continue logic below?
                        # The logic below filters: `if m["id"] > last_id`.
                        # If we set last_id = target - 1, then target > last_id is TRUE.
                        # So we can just set local variable last_id and proceed!
                        last_id = target_id
                    else:
                        # All messages are older than start_date? Or no dates found?
                        # If all are older, we don't want any. Set to max_id.
                        # If all are newer? (Unlikely unless date is very old).
                        # If no dates found, fallback to max_id.
                        max_id = max(m["id"] for m in messages)
                        self.persistence["channels"][channel]["last_post_id"] = max_id
                        self._save_persistence()
                        logger.info(f"No match for start date {start_date} (or all older). Skipping history.")
                        return 
                else:
                    # No start date, default to skipping history
                    max_id = max(m["id"] for m in messages)
                    self.persistence["channels"][channel]["last_post_id"] = max_id
                    self._save_persistence()
                    logger.info(f"Initialized channel {channel}. Setting baseline to message ID {max_id}. Future messages will be forwarded.")
                    return

            # Filter new messages
            new_msgs = [m for m in messages if m["id"] > last_id and m["id"] not in forwarded]
            
            if not new_msgs:
                return

            filter_keywords = self.config.get("filter_keywords", [])
            filter_regex = self.config.get("filter_regex", "")

            for msg in new_msgs:
                try:
                    # Content Filtering
                    text_content = msg.get("text", "")
                    
                    # 1. Keyword Filter
                    if filter_keywords:
                        found_keyword = False
                        for kw in filter_keywords:
                            if kw and kw in text_content:
                                logger.warning(f"Message {msg['id']} filtered by keyword: {kw}")
                                found_keyword = True
                                break
                        if found_keyword:
                            last_id = max(last_id, msg["id"])
                            forwarded.add(msg["id"])
                            continue

                    # 2. Regex Filter
                    if filter_regex:
                        try:
                            if re.search(filter_regex, text_content, re.IGNORECASE | re.DOTALL):
                                logger.warning(f"Message {msg['id']} filtered by regex")
                                last_id = max(last_id, msg["id"])
                                forwarded.add(msg["id"])
                                continue
                        except Exception as e:
                            logger.error(f"Invalid regex pattern: {e}")

                    # 1. Forward to Telegram
                    if self.config.get("target_channel") and self.config.get("bot_token"):
                        try:
                            channel_display = f"From #{channel}:\n"
                            await self.send_message_tg(
                                self.config["bot_token"],
                                self.config["target_channel"],
                                channel_display + msg["text"],
                                msg["images"],
                                self.config.get("proxy")
                            )
                        except Exception as e:
                            logger.error(f"TG Forward failed: {e}")

                    # 2. Forward to QQ
                    if self.config.get("target_qq_group"):
                        try:
                            channel_display = f"From #{channel}:\n"
                            await self.send_to_qq(
                                self.config["target_qq_group"],
                                channel_display + msg["text"],
                                msg["images"]
                            )
                        except Exception as e:
                             logger.error(f"QQ Forward failed: {e}")

                    # Update persistence
                    forwarded.add(msg["id"])
                    if msg["id"] > last_id:
                        last_id = msg["id"]
                    
                    logger.info(f"Forwarded post {msg['id']} from {channel}")
                    await asyncio.sleep(2) # Rate limit

                except Exception as e:
                    logger.error(f"Error processing message {msg['id']}: {e}")

            # Save state
            self.persistence["channels"][channel]["last_post_id"] = last_id
            self.persistence["channels"][channel]["forwarded_ids"] = list(forwarded)[-500:]
            self._save_persistence()

        except Exception as e:
            logger.error(f"Check updates failed for {channel}: {e}")

    # ================= Logic Ported from forwarder.py =================

    async def get_channel_messages(self, channel: str, proxy: Optional[str] = None) -> list:
        """From forwarder.py: Scraping logic"""
        url = f"https://t.me/s/{channel}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        async with httpx.AsyncClient(proxy=proxy, timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP Error for {channel}: {e}")
                return []
            
            # Check for redirect (Telegram redirects /s/ to / if sensitive/restricted)
            if "/s/" not in str(resp.url):
                logger.warning(f"Channel '{channel}' redirects to {resp.url}. This usually means the channel is restricted (sensitive/porn/piracy) and hides the public preview. The plugin cannot scrape it.")
                return []

            html = resp.text
        
        messages = []
        msg_pattern = r'data-post="([^"]+)"'
        text_pattern = r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>'
        
        posts = re.findall(msg_pattern, html)
        
        for post_id in posts:
            try:
                msg_id = int(post_id.split("/")[-1])
            except:
                continue
            
            msg_start = html.find(f'data-post="{post_id}"')
            if msg_start == -1: continue
            
            msg_end = html.find('tgme_widget_message_wrap', msg_start + 100)
            if msg_end == -1: msg_end = len(html)
            
            msg_html = html[msg_start:msg_end]
            
            # Extract text
            text_match = re.search(text_pattern, msg_html, re.DOTALL)
            text = ""
            if text_match:
                text = text_match.group(1)
                text = re.sub(r'<br\s*\/?>', '\n', text)
                text = re.sub(r'<a[^>]+href="\?q=[^"]+"[^>]*>(.*?)</a>', r'\1', text) # Remove hashtag links
                
                # Simple HTML tag stripper/converter
                clean_text = ""
                i = 0
                while i < len(text):
                    if text[i] == '<':
                        end = text.find('>', i)
                        if end == -1:
                            clean_text += text[i:]
                            break
                        tag_content = text[i+1:end].lower()
                        is_allowed = False
                        if tag_content.startswith('a h') or tag_content == '/a': is_allowed = True
                        
                        if is_allowed:
                            if tag_content == 'strong': clean_text += '<b>'
                            elif tag_content == '/strong': clean_text += '</b>'
                            elif tag_content == 'em': clean_text += '<i>'
                            elif tag_content == '/em': clean_text += '</i>'
                            else: clean_text += text[i:end+1]
                        
                        i = end + 1
                    else:
                        clean_text += text[i]
                        i += 1
                text = clean_text.strip()

            # Extract images
            raw_images = re.findall(r'background-image:url\([\'"]([^\'"]+)[\'"]\)', msg_html)
            images = []
            seen = set()
            for img in raw_images:
                if img in seen: continue
                seen.add(img)
                if "telegram.org/img/emoji" in img or "user_photo" in img: continue
                if img.startswith("//"): img = "https:" + img
                images.append(img)
            
            # Extract date
            date_pattern = r'<time datetime="([^"]+)"'
            date_match = re.search(date_pattern, msg_html)
            msg_date = None
            if date_match:
                try:
                    # ISO8601: 2025-01-31T05:24:25+00:00
                    dt_str = date_match.group(1)
                    # Parse to datetime object
                    from datetime import datetime # Added import here for self-contained snippet
                    msg_date = datetime.fromisoformat(dt_str)
                except:
                    pass

            if text or images:
                messages.append({
                    "id": msg_id,
                    "text": text,
                    "images": images,
                    "post_id": post_id,
                    "date": msg_date
                })
        
        return sorted(messages, key=lambda x: x["id"])

    async def send_message_tg(self, bot_token: str, chat_id: str, text: str, images: list, proxy: Optional[str] = None):
        """Send to Telegram"""
        base_url = f"https://api.telegram.org/bot{bot_token}"
        async with httpx.AsyncClient(proxy=proxy, timeout=120, follow_redirects=True) as client:
            if not images:
                if text:
                    await client.post(
                        f"{base_url}/sendMessage",
                        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
                    )
                return

            # Handle images... (Simplified for brevity, but needed fully)
            # Re-implementing the image downloading logic
            valid_images_data = []
            for img_url in images[:10]:
                try:
                    r = await client.get(img_url, timeout=30)
                    r.raise_for_status()
                    valid_images_data.append((img_url, r.content))
                except:
                    pass
            
            if not valid_images_data:
                if text: await client.post(f"{base_url}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
                return

            if len(valid_images_data) == 1:
                # Single photo
                await client.post(
                    f"{base_url}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text[:1024], "parse_mode": "HTML"},
                    files={"photo": ("image.jpg", valid_images_data[0][1], "image/jpeg")}
                )
            else:
                # Media Group
                media_group = []
                files = {}
                for idx, (_, data) in enumerate(valid_images_data):
                    k = f"photo{idx}"
                    files[k] = (f"img{idx}.jpg", data, "image/jpeg")
                    item = {"type": "photo", "media": f"attach://{k}"}
                    if idx == 0 and text:
                        item["caption"] = text[:1024]
                        item["parse_mode"] = "HTML"
                    media_group.append(item)
                
                await client.post(
                    f"{base_url}/sendMediaGroup",
                    data={"chat_id": chat_id, "media": json.dumps(media_group)},
                    files=files
                )
            
            # Send remaining text
            if len(text) > 1024:
                await client.post(f"{base_url}/sendMessage", data={"chat_id": chat_id, "text": text[1024:], "parse_mode": "HTML"})

    async def send_to_qq(self, group_id: int, text: str, images: list):
        """Send to QQ (NapCat)"""
        if not group_id: return
        
        # Clean HTML for QQ
        def replace_link(match):
            url = match.group(1)
            content = match.group(2)
            return url if url in content else f"{content}: {url}"
        
        qq_text = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', replace_link, text)
        qq_text = re.sub(r'<[^>]+>', '', qq_text)
        qq_text = qq_text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
        
        message = []
        if qq_text:
            message.append({"type": "text", "data": {"text": qq_text}})
        for img in images:
            message.append({"type": "image", "data": {"file": img}})
            
        payload = {"group_id": group_id, "message": message}
        
        url = self.config.get("napcat_api_url", "http://127.0.0.1:3000/send_group_msg")
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(url, json=payload)

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("Telegram Forwarder plugin terminated.")
