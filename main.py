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

Target URL: {url}
Context provided: {extra_context if extra_context else "No extra text available."}

Task:
1. Extract or write a clean, exact descriptive title.
2. Write a short 1-2 sentence description (excerpt).
3. Write a 'note' field using Markdown bullet points. Keep it strictly between 2 to 4 lines maximum. **IMPORTANT: You must strictly escape newlines as \\n inside the JSON string to prevent parsing errors.**
4. Generate 3 to 5 highly relevant, specific lowercase tags.
5. Choose the BEST matching folder from this exact list: [{folders_str}]. If none fit, return "Unsorted".

Return ONLY a valid JSON object matching this structure:
{{
  "title": "Exact Clean Title",
  "excerpt": "Short 1-2 sentence description summary.",
  "note": "- Point 1\\n- Point 2\\n- Point 3",
  "tags": ["tag1", "tag2", "tag3"],
  "folder": "Exact Folder Name"
}}
""".strip()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0
        }
    }
    
    try:
        res = requests.post(gemini_endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        res.raise_for_status()
        raw_text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        
        parsed = _extract_json_from_text(raw_text)
        if not parsed: raise ValueError("Could not parse Gemini JSON")
        return parsed
    except Exception as e:
        logging.exception("Gemini Processing Error: %s", e)
        return {"title": "Saved Link", "excerpt": "Saved via Telegram", "note": "", "tags": ["telegram"], "folder": "Unsorted"}

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

def reply_telegram(chat_id: int, message: str) -> None:
    if not TELEGRAM_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=15)
    except Exception:
        pass

def process_bookmark(chat_id: int, url: str) -> None:
    collections_map = get_raindrop_collections()
    
    extra_context = ""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower: extra_context = get_youtube_details(url)
    elif "reddit.com" in url_lower: extra_context = get_reddit_text(url)
    else: extra_context = get_website_metadata(url)

    ai_data = analyze_with_gemini(url, extra_context, list(collections_map.keys()))
    
    title = str(ai_data.get("title", "Saved Bookmark")).strip()
    excerpt = str(ai_data.get("excerpt", "")).strip()
    note = str(ai_data.get("note", "")).strip()
    folder_choice = str(ai_data.get("folder", "Unsorted")).strip()
    
    tags = ai_data.get("tags", ["telegram"])
    if not isinstance(tags, list): tags = ["telegram"]
    tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]

    success = save_to_raindrop(url, title, excerpt, note, tags, collections_map.get(folder_choice, -1))

    if success:
        msg = f"✅ Saved to Raindrop!\n\n📌 Title: {title}\n📁 Folder: {folder_choice}\n📝 Summary: {excerpt}\n🏷️ Tags: {', '.join(tags)}"
        reply_telegram(chat_id, msg)
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

        if text.lower().startswith(("/search", "/find")):
            parts = text.split(" ", 1)
            if len(parts) > 1 and parts[1].strip():
                reply_telegram(chat_id, search_raindrop(parts[1].strip()))
            else:
                reply_telegram(chat_id, "ℹ️ Usage: `/search <keyword or #tag>`")
            return {"status": "ok"}

        url_match = re.search(r"https?://\S+", text)
        if url_match:
            background_tasks.add_task(process_bookmark, chat_id, url_match.group(0).rstrip(").,]"))
            return {"status": "ok"}
    return {"status": "ok"}