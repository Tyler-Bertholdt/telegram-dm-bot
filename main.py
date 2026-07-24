import os
import re
import json
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from youtube_transcript_api import YouTubeTranscriptApi

app = FastAPI()

# Fetch secret keys from environment variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RAINDROP_TOKEN = os.environ.get("RAINDROP_TOKEN")


# --- HELPER FUNCTIONS ---

def get_youtube_text(url: str) -> str:
    """Extracts transcript text from YouTube URLs if available."""
    video_id_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
    if not video_id_match:
        return ""
    video_id = video_id_match.group(1)
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        text = " ".join([item['text'] for item in transcript[:50]])  # First 50 captions
        return f"YouTube Transcript Summary: {text}"
    except Exception:
        return "YouTube Video (No transcript available)"


def get_reddit_text(url: str) -> str:
    """Appends .json to Reddit URLs to fetch post title & content without login."""
    try:
        clean_url = url.split("?")[0].rstrip("/") + ".json"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(clean_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            post = data[0]["data"]["children"][0]["data"]
            title = post.get("title", "")
            selftext = post.get("selftext", "")[:500]
            return f"Reddit Title: {title}\nPost Body: {selftext}"
    except Exception:
        pass
    return ""


def analyze_with_gemini(url: str, extra_context: str) -> dict:
    """Calls the Gemini API to analyze link content and extract clean title + tags."""
    gemini_endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    You are a bookmark classification assistant.
    Target URL: {url}
    Context/Content: {extra_context if extra_context else "No extra text available."}

    Task: Generate a clean, descriptive title for this bookmark and 2 to 4 concise tags.
    Output format: Return ONLY a valid JSON object matching this structure:
    {{
      "title": "Clean Descriptive Title",
      "tags": ["tag1", "tag2", "tag3"]
    }}
    Do not add markdown formatting, markdown blocks (```json), or extra text outside the JSON.
    """

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }

    try:
        res = requests.post(gemini_endpoint, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Clean up potential markdown formatting wrapping JSON
            cleaned_json = re.sub(r"^```json\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
            return json.loads(cleaned_json)
    except Exception as e:
        print(f"Gemini Processing Error: {e}")

    # Fallback default if AI fails
    return {"title": "Saved Link", "tags": ["TelegramBot"]}


def save_to_raindrop(url: str, title: str, tags: list) -> bool:
    """Posts the formatted bookmark to Raindrop.io API."""
    raindrop_api = "https://api.raindrop.io/rest/v1/raindrop"
    headers = {
        "Authorization": f"Bearer {RAINDROP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "link": url,
        "title": title,
        "tags": tags,
        "collection": {"$id": -1}  # -1 saves to 'Unsorted'
    }
    
    resp = requests.post(raindrop_api, json=payload, headers=headers, timeout=10)
    return resp.status_code == 200


def reply_telegram(chat_id: int, message: str):
    """Sends confirmation text back to the Telegram chat."""
    telegram_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(telegram_api, json={"chat_id": chat_id, "text": message})


def process_bookmark(chat_id: int, url: str):
    """Background task to fetch context, call Gemini AI, and save to Raindrop."""
    extra_context = ""
    if "youtube.com" in url or "youtu.be" in url:
        extra_context = get_youtube_text(url)
    elif "reddit.com" in url:
        extra_context = get_reddit_text(url)

    ai_data = analyze_with_gemini(url, extra_context)
    title = ai_data.get("title", "Saved Link")
    tags = ai_data.get("tags", ["Telegram"])

    success = save_to_raindrop(url, title, tags)

    if success:
        reply_telegram(chat_id, f"✅ Saved to Raindrop!\n📌 Title: {title}\n🏷️ Tags: {', '.join(tags)}")
    else:
        reply_telegram(chat_id, "❌ Failed to save bookmark to Raindrop.")


# --- WEBHOOK ENDPOINTS ---

@app.get("/")
def home():
    return {"status": "Telegram -> Gemini -> Raindrop bot is running!"}


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Listens for webhooks from Telegram and delegates tasks to the background."""
    data = await request.json()

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip()

        url_match = re.search(r"https?://\S+", text)
        if url_match:
            url = url_match.group(0)
            # Add long-running operations to background execution
            background_tasks.add_task(process_bookmark, chat_id, url)

    return {"status": "ok"}