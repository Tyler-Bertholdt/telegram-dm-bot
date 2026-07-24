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


def analyze_with_gemini(url: str, extra_context: str) -> Dict[str, Any]:
    """Call Gemini Flash to extract clean title, summary, and tags."""
    if not GEMINI_API_KEY:
        return {
            "title": "Saved Link",
            "excerpt": "Saved via Telegram Bot",
            "tags": ["telegram", "uncategorized"],
        }

    gemini_endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = f"""
You are an expert bookmark metadata extractor.

Target URL: {url}

Context/Content provided:
{extra_context if extra_context else "No extra text available."}

Task:
1. Extract or write a clean, exact descriptive title for this link based on the context.
2. Write a 1-2 sentence concise summary.
3. Generate 3 to 5 highly relevant, specific lowercase tags.

Return ONLY a valid JSON object matching this structure:
{{
  "title": "Exact Clean Title",
  "excerpt": "Short 1-2 sentence description summary.",
  "tags": ["tag1", "tag2", "tag3"]
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
            raise ValueError(f"Could not parse Gemini JSON: {raw_text[:200]}")

        title = str(parsed.get("title", "Saved Link")).strip()
        excerpt = str(parsed.get("excerpt", "Saved via Telegram Bot")).strip()
        tags = parsed.get("tags", ["telegram", "uncategorized"])

        if not isinstance(tags, list):
            tags = ["telegram", "uncategorized"]

        cleaned_tags = []
        for tag in tags:
            tag_str = str(tag).strip().lower()
            if tag_str:
                cleaned_tags.append(tag_str)

        if not cleaned_tags:
            cleaned_tags = ["telegram", "uncategorized"]

        return {
            "title": title or "Saved Link",
            "excerpt": excerpt or "Saved via Telegram Bot",
            "tags": cleaned_tags[:5],
        }

    except Exception as e:
        logging.exception("Gemini Processing Error: %s", e)
        return {
            "title": "Saved Link",
            "excerpt": "Saved via Telegram Bot",
            "tags": ["telegram", "uncategorized"],
        }


def save_to_raindrop(url: str, title: str, excerpt: str, tags: List[str]) -> bool:
    """Posts bookmark to Raindrop.io with automatic parsing enabled."""
    if not RAINDROP_TOKEN:
        logging.error("RAINDROP_TOKEN is missing")
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
        "collection": {"$id": -1},
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
        logging.error("TELEGRAM_TOKEN is missing")
        return

    telegram_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            telegram_api,
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
    except Exception as e:
        logging.exception("Telegram send error: %s", e)


def process_bookmark(chat_id: int, url: str) -> None:
    """Background execution flow."""
    extra_context = ""

    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        extra_context = get_youtube_details(url)
    elif "reddit.com" in url_lower:
        extra_context = get_reddit_text(url)

    ai_data = analyze_with_gemini(url, extra_context)
    title = ai_data.get("title", "Saved Bookmark")
    excerpt = ai_data.get("excerpt", "")
    tags = ai_data.get("tags", ["telegram"])

    if not isinstance(tags, list):
        tags = ["telegram"]

    tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]
    if not tags:
        tags = ["telegram"]

    success = save_to_raindrop(url, title, excerpt, tags)

    if success:
        reply_telegram(
            chat_id,
            f"✅ Saved to Raindrop!\n\n📌 Title: {title}\n📝 Summary: {excerpt}\n🏷️ Tags: {', '.join(tags)}",
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