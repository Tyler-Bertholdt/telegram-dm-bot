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

# --- HELPER FUNCTIONS ---

def get_raindrop_collections() -> Dict[str, int]:
    """Fetches user's Raindrop collections so Gemini can choose one."""
    if not RAINDROP_TOKEN:
        return {}
    
    url = "https://api.raindrop.io/rest/v1/collections"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            # Returns a dictionary like: {"YouTube Videos": 123456, "Articles": 78910}
            return {item["title"]: item["_id"] for item in items}
    except Exception as e:
        logging.warning("Error fetching collections: %s", e)
    return {}


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
    """Fetch YouTube title, channel, and transcript snippet if available."""
    context_parts: List[str] = []

    try:
        res = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=5,
        )
        if res.status_code == 200:
            data = res.json()
            title = data.get("title")
            author = data.get("author_name")
            if title:
                context_parts.append(f"Video Title: {title}")
            if author:
                context_parts.append(f"Channel: {author}")
    except Exception as e:
        logging.warning("YouTube oEmbed error: %s", e)

    video_id = extract_youtube_video_id(url)
    if video_id:
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            text = " ".join(item.get("text", "") for item in transcript[:40]).strip()
            if text:
                context_parts.append(f"Transcript Snippet: {text}")
        except Exception:
            context_parts.append("Transcript: Not available for this video.")

    return "\n".join(context_parts) if context_parts else "YouTube Video"


def get_reddit_text(url: str) -> str:
    """Fetch Reddit title and body via public JSON endpoint."""
    try:
        clean_url = url.split("?")[0].rstrip("/") + ".json"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(clean_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            post = data[0]["data"]["children"][0]["data"]
            title = post.get("title", "")
            selftext = post.get("selftext", "")[:500]
            parts = []
            if title:
                parts.append(f"Reddit Post Title: {title}")
            if selftext:
                parts.append(f"Post Body: {selftext}")
            return "\n".join(parts)
    except Exception as e:
        logging.warning("Reddit fetch error: %s", e)

    return ""


def get_website_metadata(url: str) -> str:
    """Fetches title and description from general web pages (Instagram, Twitter, Blogs, etc.)."""
    try:
        # Spoofing a Facebook/WhatsApp crawler forces sites like Instagram to return preview data
        headers = {"User-Agent": "facebookexternalhit/1.1"}
        resp = requests.get(url, headers=headers, timeout=5)
        
        if resp.status_code == 200:
            html = resp.text
            
            # Extract OpenGraph Title (usually contains IG caption or exact title)
            title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if not title_match:
                title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""
            
            # Extract OpenGraph Description
            desc_match = re.search(r'<meta\s+(?:property="og:description"|name="description")\s+content="([^"]+)"', html, re.IGNORECASE)
            desc = desc_match.group(1).strip() if desc_match else ""

            parts = []
            if title: 
                parts.append(f"Webpage Title: {title}")
            if desc: 
                parts.append(f"Webpage Description: {desc}")
            
            return "\n".join(parts)
    except Exception as e:
        logging.warning("Website metadata fetch error: %s", e)
        
    return ""


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON extraction from Gemini text output."""
    text = text.strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None

    return None


def analyze_with_gemini(url: str, extra_context: str, available_folders: List[str]) -> Dict[str, Any]:
    """Call Gemini Flash to extract clean title, summary, tags, and folder choice."""
    if not GEMINI_API_KEY:
        return {"title": "Saved Link", "excerpt": "Saved via Telegram Bot", "tags": ["telegram"], "folder": "Unsorted"}

    gemini_endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    )

    folders_str = ", ".join(available_folders) if available_folders else "None available (use Unsorted)"

    prompt = f"""
You are an expert bookmark metadata extractor.

Target URL: {url}
Context provided: {extra_context if extra_context else "No extra text available."}

Task:
1. Extract or write a clean, exact descriptive title.
2. Write a 1-2 sentence concise summary.
3. Generate 3 to 5 highly relevant, specific lowercase tags.
4. Choose the BEST matching folder from this exact list of the user's folders: [{folders_str}]. If none fit, return "Unsorted".

Return ONLY a valid JSON object matching this structure:
{{
  "title": "Exact Clean Title",
  "excerpt": "Short 1-2 sentence description summary.",
  "tags": ["tag1", "tag2", "tag3"],
  "folder": "Exact Folder Name"
}}
Do not add markdown formatting or markdown blocks.
""".strip()

    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        res = requests.post(gemini_endpoint, json=payload, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()

        raw_text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        ).strip()

        parsed = _extract_json_from_text(raw_text)
        if not parsed:
            raise ValueError("Could not parse Gemini JSON")

        return parsed

    except Exception as e:
        logging.exception("Gemini Processing Error: %s", e)
        return {"title": "Saved Link", "excerpt": "Saved via Telegram", "tags": ["telegram"], "folder": "Unsorted"}


def save_to_raindrop(url: str, title: str, excerpt: str, tags: List[str], collection_id: int) -> bool:
    """Posts bookmark to Raindrop.io with automatic parsing and dynamic folder routing."""
    if not RAINDROP_TOKEN:
        return False

    raindrop_api = "https://api.raindrop.io/rest/v1/raindrop"
    headers = {
        "Authorization": f"Bearer {RAINDROP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "link": url,
        "title": title,
        "excerpt": excerpt,
        "tags": tags,
        "pleaseParse": {},
        "collection": {"$id": collection_id},
    }

    try:
        resp = requests.post(raindrop_api, json=payload, headers=headers, timeout=15)
        return resp.status_code in (200, 201)
    except Exception as e:
        logging.exception("Raindrop Exception: %s", e)
        return False


def reply_telegram(chat_id: int, message: str) -> None:
    """Send confirmation back to Telegram."""
    if not TELEGRAM_TOKEN:
        return

    telegram_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(telegram_api, json={"chat_id": chat_id, "text": message}, timeout=15)
    except Exception as e:
        logging.exception("Telegram send error: %s", e)


def process_bookmark(chat_id: int, url: str) -> None:
    """Background execution flow."""
    # 1. Fetch User's Folders
    collections_map = get_raindrop_collections()
    folder_names = list(collections_map.keys())

    # 2. Get Context
    extra_context = ""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        extra_context = get_youtube_details(url)
    elif "reddit.com" in url_lower:
        extra_context = get_reddit_text(url)
    else:
        # Fallback for Instagram, Twitter, News Articles, Blogs, etc.
        extra_context = get_website_metadata(url)

    # 3. Analyze with Gemini
    ai_data = analyze_with_gemini(url, extra_context, folder_names)
    
    title = str(ai_data.get("title", "Saved Bookmark")).strip()
    excerpt = str(ai_data.get("excerpt", "")).strip()
    folder_choice = str(ai_data.get("folder", "Unsorted")).strip()
    
    tags = ai_data.get("tags", ["telegram"])
    if not isinstance(tags, list):
        tags = ["telegram"]
    tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]
    if not tags:
        tags = ["telegram"]

    # 4. Map Folder Name to ID (-1 is the default fallback for Unsorted)
    collection_id = collections_map.get(folder_choice, -1)

    # 5. Save
    success = save_to_raindrop(url, title, excerpt, tags, collection_id)

    if success:
        reply_telegram(
            chat_id,
            f"✅ Saved to Raindrop!\n\n📌 Title: {title}\n📁 Folder: {folder_choice}\n📝 Summary: {excerpt}\n🏷️ Tags: {', '.join(tags)}",
        )
    else:
        reply_telegram(chat_id, "❌ Failed to save bookmark to Raindrop.")


# --- ENDPOINTS ---

@app.get("/")
def home():
    return {"status": "Bot is active!"}


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip()

        url_match = re.search(r"https?://\S+", text)
        if url_match:
            url = url_match.group(0).rstrip(").,]")
            background_tasks.add_task(process_bookmark, chat_id, url)

    return {"status": "ok"}