import os
import re
import json
import logging
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Request, BackgroundTasks
from youtube_transcript_api import YouTubeTranscriptApi

app = FastAPI()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RAINDROP_TOKEN = os.environ.get("RAINDROP_TOKEN")

logging.basicConfig(level=logging.INFO)

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, RAINDROP_TOKEN]):
    logging.warning(
        "Missing one or more required environment variables: "
        "TELEGRAM_TOKEN, GEMINI_API_KEY, RAINDROP_TOKEN"
    )

HELP_TEXT = """
🤖 *Bookmark Bot Commands & Guide*

**Commands:**
• `/help` or `/start` - Show this guide.
• `/search <query>` - Search your Raindrop bookmarks.
• `/link <url>` - Manually specify the URL.
• `/folder <folder_name>` - Manually pick a Raindrop collection folder.
• `/text <your text>` - Add custom commentary, notes, or caption text.

**📁 File Support:**
• Upload a `.txt` or `.md` file directly to the bot! You can add captions like `/folder Notes $no-caption` when uploading.

**Modifier Flags (Include anywhere in message or caption):**
• `$no-caption` - Skip saving a short caption/excerpt.
• `$no-summary` - Skip AI bullet-point note summary generation.
• `$no-folder` - Save to **Unsorted** (bypasses folder selection).
• `$no-link` - Omit saving the URL link.

**Usage Examples:**
1. Direct YouTube link without https://:
   `youtube.com/watch?v=xyz /folder Videos /text Great documentary`

2. Upload a `.txt` or `.md` file:
   Upload `notes.md` with caption `/folder Research $no-caption`
"""

# --- Raindrop Helpers ---

def get_raindrop_collections() -> Dict[str, int]:
    if not RAINDROP_TOKEN:
        return {}
    url = "https://api.raindrop.io/rest/v1/collections"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            return {item["title"]: item["_id"] for item in items}
    except Exception as e:
        logging.warning("Error fetching collections: %s", e)
    return {}

def search_raindrop(query: str) -> str:
    if not RAINDROP_TOKEN:
        return "❌ Raindrop token is missing."
    url = "https://api.raindrop.io/rest/v1/raindrops/0"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    params = {"search": query, "perpage": 5}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if not items:
                return f"🤷‍♂️ No bookmarks found for '{query}'"
            reply_text = f"🔍 Top results for '{query}':\n\n"
            for idx, item in enumerate(items, 1):
                title = item.get("title", "Untitled Bookmark")
                link = item.get("link", "")
                reply_text += f"{idx}. {title}\n🔗 {link}\n\n"
            return reply_text.strip()
    except Exception as e:
        logging.warning("Error searching Raindrop: %s", e)
    return "❌ Error searching Raindrop."

def save_to_raindrop(url: str, title: str, excerpt: str, note: str, tags: List[str], collection_id: int) -> bool:
    if not RAINDROP_TOKEN:
        return False
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "link": url,
        "title": title,
        "excerpt": excerpt,
        "note": note,
        "tags": tags,
        "pleaseParse": {},
        "collection": {"$id": collection_id},
    }
    try:
        resp = requests.post("https://api.raindrop.io/rest/v1/raindrop", json=payload, headers=headers, timeout=15)
        return resp.status_code in (200, 201)
    except Exception as e:
        logging.exception("Raindrop Exception: %s", e)
        return False

# --- Telegram File Extractor ---

def get_telegram_file_text(file_id: str) -> str:
    if not TELEGRAM_TOKEN:
        return ""
    try:
        # Step 1: Get file path from Telegram
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        resp = requests.get(get_file_url, timeout=10)
        if resp.status_code == 200:
            file_path = resp.json().get("result", {}).get("file_path")
            if file_path:
                # Step 2: Download file content
                dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                dl_resp = requests.get(dl_url, timeout=15)
                if dl_resp.status_code == 200:
                    return dl_resp.content.decode("utf-8", errors="ignore")
    except Exception as e:
        logging.warning("Error downloading file from Telegram: %s", e)
    return ""

# --- Content Scrapers ---

def extract_youtube_video_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=)([0-9A-Za-z_-]{11})",
        r"(?:youtu\.be/)([0-9A-Za-z_-]{11})",
        r"(?:shorts/)([0-9A-Za-z_-]{11})",
        r"(?:embed/)([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_youtube_details(url: str) -> str:
    context_parts: List[str] = []
    try:
        res = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("title"): context_parts.append(f"Video Title: {data.get('title')}")
            if data.get("author_name"): context_parts.append(f"Channel: {data.get('author_name')}")
    except Exception:
        pass

    video_id = extract_youtube_video_id(url)
    if video_id:
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            text = " ".join(item.get("text", "") for item in transcript[:40]).strip()
            if text: context_parts.append(f"Transcript Snippet: {text}")
        except Exception:
            context_parts.append("Transcript: Not available.")
    return "\n".join(context_parts) if context_parts else "YouTube Video"

def get_reddit_text(url: str) -> str:
    try:
        clean_url = url.split("?")[0].rstrip("/") + ".json"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(clean_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            post = resp.json()[0]["data"]["children"][0]["data"]
            parts = []
            if post.get("title"): parts.append(f"Reddit Post Title: {post.get('title')}")
            if post.get("selftext"): parts.append(f"Post Body: {post.get('selftext')[:500]}")
            return "\n".join(parts)
    except Exception:
        pass
    return ""

def get_website_metadata(url: str) -> str:
    try:
        resp = requests.get(f"https://api.microlink.io?url={url}", timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            parts = []
            if data.get("title"): parts.append(f"Webpage Title: {data.get('title')}")
            if data.get("description"): parts.append(f"Webpage Description: {data.get('description')}")
            return "\n".join(parts)
    except Exception:
        pass
    return ""

# --- Gemini Processing ---

def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict): return parsed
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict): return parsed
        except Exception:
            return None
    return None

def analyze_with_gemini(url: str, extra_context: str, available_folders: List[str]) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"title": "Saved Link", "excerpt": "Saved via Telegram", "note": "", "tags": ["telegram"], "folder": "Unsorted"}

    gemini_endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    folders_str = ", ".join(available_folders) if available_folders else "None available (use Unsorted)"

    prompt = f"""
You are an expert bookmark metadata extractor.

Target URL/Topic: {url}
Context provided: {extra_context if extra_context else "No extra text available."}

Task:
1. Extract or write a clean, exact descriptive title.
2. Write a short 1-2 sentence description (excerpt).
3. Write a 'note' field using Markdown bullet points (2 to 4 lines maximum). Strictly escape newlines as \\n inside JSON.
4. Generate 3 to 5 highly relevant lowercase tags.
5. Choose the BEST matching folder from this list: [{folders_str}]. If none fit, return "Unsorted".

Return ONLY a valid JSON object matching this structure:
{{
  "title": "Exact Clean Title",
  "excerpt": "Short 1-2 sentence description summary.",
  "note": "- Point 1\\n- Point 2",
  "tags": ["tag1", "tag2"],
  "folder": "Exact Folder Name"
}}
""".strip()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0}
    }
    
    try:
        res = requests.post(gemini_endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        res.raise_for_status()
        raw_text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        parsed = _extract_json_from_text(raw_text)
        if not parsed: raise ValueError("Could not parse Gemini JSON response")
        return parsed
    except Exception as e:
        logging.exception("Gemini Processing Error: %s", e)
        return {"title": "Saved Content", "excerpt": "Saved via Telegram", "note": "", "tags": ["telegram"], "folder": "Unsorted"}

# --- Input Parser ---

def extract_url(text: str) -> Optional[str]:
    # Matches http://, https://, www., or standard domains (e.g. youtube.com/watch)
    url_pattern = r"(?:https?://|www\.)[^\s]+|(?:[a-zA-Z0-9-]+\.)+(?:com|org|net|io|co|app|me|dev|edu|gov|ai|tv|be|site|tech|xyz|info)(?:/[^\s]*)?"
    match = re.search(url_pattern, text, re.IGNORECASE)
    if match:
        url = match.group(0).rstrip(").,]")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url
    return None

def parse_user_input(text: str) -> dict:
    text_lower = text.lower()
    
    # Flags extraction
    no_caption = "$no-caption" in text_lower
    no_summary = "$no-summary" in text_lower
    no_folder = "$no-folder" in text_lower
    no_link = "$no-link" in text_lower

    # Clean flags out for parameter parsing
    cleaned_text = re.sub(r"\$no-(caption|summary|folder|link)", "", text, flags=re.IGNORECASE).strip()

    # Extract /link <url>
    link_match = re.search(r"/link\s+([^\s]+)", cleaned_text, re.IGNORECASE)
    manual_link = None
    if link_match:
        raw_link = link_match.group(1).rstrip(").,]")
        manual_link = "https://" + raw_link if not raw_link.startswith(("http://", "https://")) else raw_link

    if not manual_link and not no_link:
        manual_link = extract_url(cleaned_text)

    # Extract /folder <folder_name>
    folder_match = re.search(r"/folder\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_folder = folder_match.group(1).strip() if folder_match else None

    # Extract /text <additional text>
    text_match = re.search(r"/text\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_text = text_match.group(1).strip() if text_match else None

    # Fallback for extra context commentary when /text isn't explicitly typed
    if not manual_text:
        temp = re.sub(r"/link\s+[^\s]+", "", cleaned_text, flags=re.IGNORECASE)
        temp = re.sub(r"/folder\s+([^\/\$\n]+)", "", temp, flags=re.IGNORECASE)
        if manual_link:
            temp = temp.replace(manual_link, "").replace(manual_link.replace("https://", ""), "")
        extra_commentary = temp.strip()
        if extra_commentary:
            manual_text = extra_commentary

    return {
        "url": None if no_link else manual_link,
        "folder": manual_folder,
        "text": manual_text or "",
        "no_caption": no_caption,
        "no_summary": no_summary,
        "no_folder": no_folder,
        "no_link": no_link
    }

def reply_telegram(chat_id: int, message: str) -> None:
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=15
        )
        if res.status_code != 200:
            # Fallback to plain text if Markdown parsing fails
            requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=15)
    except Exception as e:
        logging.warning("Error sending Telegram message: %s", e)

def process_bookmark(chat_id: int, parsed_opts: dict) -> None:
    try:
        url = parsed_opts["url"] or ""
        manual_folder = parsed_opts["folder"]
        manual_text = parsed_opts["text"]
        file_text = parsed_opts.get("file_text", "")
        no_caption = parsed_opts["no_caption"]
        no_summary = parsed_opts["no_summary"]
        no_folder = parsed_opts["no_folder"]
        no_link = parsed_opts["no_link"]

        if not url and not manual_text and not file_text:
            reply_telegram(chat_id, "⚠️ No valid link, text, or file content was provided.")
            return

        collections_map = get_raindrop_collections()

        extra_context = ""
        if url:
            url_lower = url.lower()
            if "youtube.com" in url_lower or "youtu.be" in url_lower:
                extra_context = get_youtube_details(url)
            elif "reddit.com" in url_lower:
                extra_context = get_reddit_text(url)
            else:
                extra_context = get_website_metadata(url)

        if manual_text:
            extra_context += f"\nManual User Text/Caption: {manual_text}"

        if file_text:
            # Include attached file text (truncating if extremely large)
            truncated_file_text = file_text[:8000]
            extra_context += f"\nAttached File Content:\n{truncated_file_text}"

        # Analyze metadata with Gemini
        ai_data = analyze_with_gemini(url or "Text Document", extra_context, list(collections_map.keys()))

        title = str(ai_data.get("title", "Saved Bookmark")).strip()
        excerpt = "" if no_caption else str(ai_data.get("excerpt", "")).strip()
        note = "" if no_summary else str(ai_data.get("note", "")).strip()

        # Determine collection folder
        if no_folder:
            folder_choice = "Unsorted"
        elif manual_folder:
            folder_choice = manual_folder
        else:
            folder_choice = str(ai_data.get("folder", "Unsorted")).strip()

        tags = ai_data.get("tags", ["telegram"])
        if not isinstance(tags, list): tags = ["telegram"]
        tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]

        target_url = "https://telegram.org" if (no_link or not url) else url
        collection_id = collections_map.get(folder_choice, -1) if folder_choice != "Unsorted" else -1

        success = save_to_raindrop(target_url, title, excerpt, note, tags, collection_id)

        if success:
            msg = f"✅ *Saved to Raindrop!*\n\n📌 *Title:* {title}\n📁 *Folder:* {folder_choice}"
            if excerpt:
                msg += f"\n📝 *Excerpt:* {excerpt}"
            if note:
                msg += f"\n📓 *Notes:*\n{note}"
            msg += f"\n🏷️ *Tags:* {', '.join(tags)}"
            reply_telegram(chat_id, msg)
        else:
            reply_telegram(chat_id, "❌ Failed to save bookmark to Raindrop. Please check your Raindrop token.")
    except Exception as e:
        logging.exception("Error in process_bookmark: %s", e)
        reply_telegram(chat_id, f"❌ An error occurred while processing:\n`{str(e)}`")

# --- API Endpoints ---

@app.get("/")
def home():
    return {"status": "Bot is active!"}

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if "message" in data:
            msg = data["message"]
            chat_id = msg["chat"]["id"]
            
            # Message text or document caption
            text = msg.get("text", "") or msg.get("caption", "")
            text = text.strip()

            # Handle /help and /start
            if text.lower().startswith(("/help", "/start")):
                reply_telegram(chat_id, HELP_TEXT)
                return {"status": "ok"}

            # Handle /search
            if text.lower().startswith(("/search", "/find")):
                parts = text.split(" ", 1)
                if len(parts) > 1 and parts[1].strip():
                    reply_telegram(chat_id, search_raindrop(parts[1].strip()))
                else:
                    reply_telegram(chat_id, "ℹ️ Usage: `/search <keyword or #tag>`")
                return {"status": "ok"}

            # File upload processing
            file_text = ""
            if "document" in msg:
                doc = msg["document"]
                file_name = doc.get("file_name", "").lower()
                mime_type = doc.get("mime_type", "").lower()

                if file_name.endswith((".txt", ".md", ".markdown")) or "text" in mime_type:
                    file_id = doc.get("file_id")
                    if file_id:
                        file_text = get_telegram_file_text(file_id)
                        if not file_text:
                            reply_telegram(chat_id, "❌ Could not read content from the uploaded file.")
                            return {"status": "ok"}
                else:
                    reply_telegram(chat_id, "⚠️ Unsupported file type. Please upload a `.txt` or `.md` file.")
                    return {"status": "ok"}

            # Parse options & flags
            parsed_opts = parse_user_input(text)
            if file_text:
                parsed_opts["file_text"] = file_text

            if parsed_opts["url"] or parsed_opts["text"] or parsed_opts.get("file_text"):
                background_tasks.add_task(process_bookmark, chat_id, parsed_opts)
                return {"status": "ok"}

    except Exception as e:
        logging.exception("Error in webhook: %s", e)

    return {"status": "ok"}