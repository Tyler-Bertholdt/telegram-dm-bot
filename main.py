import os
import re
import json
import logging
import difflib
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
🤖 *Advanced Bookmark Bot - Usage & Rules Guide*

**📌 Core Features:**
• **Paste any URL**: Saved, summarized, and categorized automatically.
• **File Uploads**: Send `.txt` or `.md` files directly to summarize document content.
• **Smart Search**: Use `/search` with NLP fuzzy tag suggestions and custom result limits.

**💬 Available Commands:**
• `/help` or `/start` - Show this guide.
• `/search <query> [/result <1-50>]` - Search bookmarks (e.g. `/search python /result 10`).
• `/link <url>` - Manually set the main bookmark link URL.
• `/folder <folder_name>` - Pick or auto-create a Raindrop collection.
• `/text <your text>` - Add priority text/context (URLs here stay inside notes).
• `/tags <tag1, tag2>` - Manually assign custom tags.
• `/prompt <instructions>` - Provide custom instructions to Gemini AI.
• `/limit <number>` or `/words <number>` - Set strict word/line limits for notes.

**⚡ Modifier Flags & Override Rules:**
• `$no-tags` - Suppress AI tags.
  └ *Rule:* Using `/tags python, ai $no-tags` saves ONLY `python` & `ai` and ignores AI tags.
• `$no-folder` - Suppress AI folder selection.
  └ *Rule:* Using `/folder Tech $no-folder` forces saving to `Tech` and ignores AI choice.
• `$no-caption` - Skip short preview caption/excerpt on the card.
• `$no-summary` - Skip AI bullet-point note generation.
• `$no-link` - Omit saving the main URL link.

**⚙️ Automated System Rules:**
1. **Auto Folder Creation**: If a `/folder` doesn't exist in Raindrop, it is created automatically.
2. **Text Priority**: Input in `/text` is given highest priority over scraped website details.
3. **URL Isolation**: Links inside `/text` remain in the notes and won't overwrite the main bookmark URL.
4. **File Processing**: Text inside uploaded `.txt` / `.md` files is merged directly into AI context.

**💡 Usage Examples:**
1. Search with Custom Result Count:
   `/search python /result 10`

2. Custom Prompt + Word Limit:
   `youtube.com/watch?v=xyz /folder Tech /prompt Focus on coding tips /limit 25`

3. Manual Tags Override:
   `https://github.com /tags dev, tools $no-tags` *(Only saves 'dev' and 'tools')*
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

def get_or_create_collection(folder_name: str) -> int:
    if not RAINDROP_TOKEN or not folder_name or folder_name.lower() == "unsorted":
        return -1
    
    # Check existing collections
    collections_map = get_raindrop_collections()
    for title, cid in collections_map.items():
        if title.lower() == folder_name.lower():
            return cid
            
    # Create new collection if it doesn't exist
    url = "https://api.raindrop.io/rest/v1/collection"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}", "Content-Type": "application/json"}
    payload = {"title": folder_name}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            item = resp.json().get("item", {})
            return item.get("_id", -1)
    except Exception as e:
        logging.warning("Error creating Raindrop collection: %s", e)
    
    return -1

def get_raindrop_tags() -> List[str]:
    if not RAINDROP_TOKEN:
        return []
    url = "https://api.raindrop.io/rest/v1/tags/0"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            return [item["_id"] for item in items if "_id" in item]
    except Exception as e:
        logging.warning("Error fetching Raindrop tags: %s", e)
    return []

def search_raindrop(query_text: str) -> str:
    if not RAINDROP_TOKEN:
        return "❌ Raindrop token is missing."
    
    # Parse /result <num> or /limit <num> parameter if provided
    perpage = 5  # default
    result_match = re.search(r"/(?:result|results|limit|count)\s+(\d+)", query_text, re.IGNORECASE)
    if result_match:
        perpage = min(max(int(result_match.group(1)), 1), 50)
        clean_query = re.sub(r"/(?:result|results|limit|count)\s+\d+", "", query_text, flags=re.IGNORECASE).strip()
    else:
        clean_query = query_text.strip()

    if not clean_query:
        return "ℹ️ Usage: `/search <keyword or #tag> [/result <1-50>]`"

    url = "https://api.raindrop.io/rest/v1/raindrops/0"
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    params = {"search": clean_query, "perpage": perpage}
    
    # NLP / Fuzzy Tag Matching
    all_tags = get_raindrop_tags()
    search_term = clean_query.lstrip("#").lower()
    
    suggested_tags = difflib.get_close_matches(search_term, all_tags, n=5, cutoff=0.3)
    substring_tags = [t for t in all_tags if search_term in t.lower() and t not in suggested_tags]
    recommended_tags = (suggested_tags + substring_tags)[:5]
    
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            
            if items:
                reply_text = f"🔍 Top {len(items)} results for '{clean_query}':\n\n"
                for idx, item in enumerate(items, 1):
                    title = item.get("title", "Untitled Bookmark")
                    link = item.get("link", "")
                    tags_list = item.get("tags", [])
                    tag_str = f" [#{', #'.join(tags_list)}]" if tags_list else ""
                    reply_text += f"{idx}. {title}{tag_str}\n🔗 {link}\n\n"
                
                if recommended_tags:
                    reply_text += "🏷️ *Related Tags:* " + ", ".join([f"`#{t}`" for t in recommended_tags])
                return reply_text.strip()
            else:
                reply_text = f"🤷‍♂️ No exact bookmarks found for '{clean_query}'.\n\n"
                if recommended_tags:
                    reply_text += "💡 *Did you mean or try searching these tags?*\n"
                    reply_text += "\n".join([f"• `/search #{t} /result {perpage}`" for t in recommended_tags])
                else:
                    reply_text += "💡 Try searching with a broader keyword or check your Raindrop tags."
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
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        resp = requests.get(get_file_url, timeout=10)
        if resp.status_code == 200:
            file_path = resp.json().get("result", {}).get("file_path")
            if file_path:
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

def analyze_with_gemini(
    url: str,
    extra_context: str,
    available_folders: List[str],
    custom_prompt: Optional[str] = None,
    word_limit: Optional[int] = None
) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"title": "Saved Content", "excerpt": "Saved via Telegram", "note": "", "tags": ["telegram"], "folder": "Unsorted"}

    gemini_endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    folders_str = ", ".join(available_folders) if available_folders else "None available (use Unsorted)"

    limit_instruction = f"Strictly keep the 'note' summary under {word_limit} words/bullet points." if word_limit else "Keep the 'note' field between 2 to 4 bullet points."
    user_prompt_instruction = f"\nUser Custom Instruction: {custom_prompt}\nFollow the user custom instruction carefully when generating the response." if custom_prompt else ""

    prompt = f"""
You are an expert bookmark metadata extractor and summarizer.

Target URL/Topic: {url}
Context provided (NOTE: Prioritize any PRIMARY USER TEXT provided here over raw scraped metadata):
{extra_context if extra_context else "No extra text available."}
{user_prompt_instruction}

Task:
1. Extract or write a clean, exact descriptive title.
2. Write a short 1-2 sentence description (excerpt).
3. Write a 'note' field using Markdown bullet points. {limit_instruction} Strictly escape newlines as \\n inside JSON. Make sure any URLs inside user text meant for notes are kept in the note.
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
    no_tags = "$no-tags" in text_lower

    cleaned_text = re.sub(r"\$no-(caption|summary|folder|link|tags)", "", text, flags=re.IGNORECASE).strip()

    # 1. Extract /prompt <custom prompt>
    prompt_match = re.search(r"/prompt\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_prompt = prompt_match.group(1).strip() if prompt_match else None

    # 2. Extract /limit or /words <number>
    limit_match = re.search(r"/(?:limit|words)\s+(\d+)", cleaned_text, re.IGNORECASE)
    word_limit = int(limit_match.group(1)) if limit_match else None

    # 3. Extract /tags <tag1, tag2, ...>
    tags_match = re.search(r"/tags\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_tags = []
    if tags_match:
        raw_tags = tags_match.group(1).strip()
        manual_tags = [t.strip().lstrip("#").lower() for t in re.split(r"[,;]+|\s+", raw_tags) if t.strip()]

    # 4. Extract /folder <folder_name>
    folder_match = re.search(r"/folder\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_folder = folder_match.group(1).strip() if folder_match else None

    # 5. Extract /link <url>
    link_match = re.search(r"/link\s+([^\s]+)", cleaned_text, re.IGNORECASE)
    manual_link = None
    if link_match:
        raw_link = link_match.group(1).rstrip(").,]")
        manual_link = "https://" + raw_link if not raw_link.startswith(("http://", "https://")) else raw_link

    # 6. Extract /text <additional text>
    text_match = re.search(r"/text\s+([^\/\$\n]+)", cleaned_text, re.IGNORECASE)
    manual_text = text_match.group(1).strip() if text_match else None

    # 7. Extract URL outside of /text
    text_without_text_cmd = re.sub(r"/text\s+([^\/\$\n]+)", "", cleaned_text, flags=re.IGNORECASE)
    if not manual_link and not no_link:
        manual_link = extract_url(text_without_text_cmd)

    # Fallback for extra context commentary when /text isn't explicitly typed
    if not manual_text:
        temp = re.sub(r"/link\s+[^\s]+", "", cleaned_text, flags=re.IGNORECASE)
        temp = re.sub(r"/folder\s+([^\/\$\n]+)", "", temp, flags=re.IGNORECASE)
        temp = re.sub(r"/prompt\s+([^\/\$\n]+)", "", temp, flags=re.IGNORECASE)
        temp = re.sub(r"/(?:limit|words)\s+\d+", "", temp, flags=re.IGNORECASE)
        temp = re.sub(r"/tags\s+([^\/\$\n]+)", "", temp, flags=re.IGNORECASE)
        if manual_link:
            temp = temp.replace(manual_link, "").replace(manual_link.replace("https://", ""), "")
        extra_commentary = temp.strip()
        if extra_commentary:
            manual_text = extra_commentary

    return {
        "url": None if (no_link and not link_match) else manual_link,
        "folder": manual_folder,
        "text": manual_text or "",
        "prompt": manual_prompt,
        "word_limit": word_limit,
        "manual_tags": manual_tags,
        "no_caption": no_caption,
        "no_summary": no_summary,
        "no_folder": no_folder,
        "no_link": no_link,
        "no_tags": no_tags,
        "explicit_link_given": bool(link_match)
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
            requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=15)
    except Exception as e:
        logging.warning("Error sending Telegram message: %s", e)

def process_bookmark(chat_id: int, parsed_opts: dict) -> None:
    try:
        url = parsed_opts["url"] or ""
        manual_folder = parsed_opts["folder"]
        manual_text = parsed_opts["text"]
        manual_prompt = parsed_opts["prompt"]
        word_limit = parsed_opts["word_limit"]
        manual_tags = parsed_opts["manual_tags"]
        file_text = parsed_opts.get("file_text", "")
        
        no_caption = parsed_opts["no_caption"]
        no_summary = parsed_opts["no_summary"]
        no_folder = parsed_opts["no_folder"]
        no_link = parsed_opts["no_link"]
        no_tags = parsed_opts["no_tags"]
        explicit_link_given = parsed_opts["explicit_link_given"]

        if not url and not manual_text and not file_text:
            reply_telegram(chat_id, "⚠️ No valid link, text, or file content was provided.")
            return

        # Prioritize /text at top of extra_context
        extra_context = ""
        if manual_text:
            extra_context += f"PRIMARY USER TEXT/CAPTION:\n{manual_text}\n\n"

        if file_text:
            extra_context += f"ATTACHED FILE CONTENT:\n{file_text[:8000]}\n\n"

        if url:
            url_lower = url.lower()
            if "youtube.com" in url_lower or "youtu.be" in url_lower:
                extra_context += f"SCRAPED YOUTUBE DETAILS:\n{get_youtube_details(url)}"
            elif "reddit.com" in url_lower:
                extra_context += f"SCRAPED REDDIT DETAILS:\n{get_reddit_text(url)}"
            else:
                extra_context += f"SCRAPED WEBPAGE DETAILS:\n{get_website_metadata(url)}"

        collections_map = get_raindrop_collections()

        # Analyze metadata with Gemini
        ai_data = analyze_with_gemini(
            url or "Text Document",
            extra_context,
            list(collections_map.keys()),
            custom_prompt=manual_prompt,
            word_limit=word_limit
        )

        title = str(ai_data.get("title", "Saved Bookmark")).strip()
        excerpt = "" if no_caption else str(ai_data.get("excerpt", "")).strip()
        note = "" if no_summary else str(ai_data.get("note", "")).strip()

        # --- Folder Selection & Auto-Creation ---
        if manual_folder:
            folder_choice = manual_folder
        elif no_folder:
            folder_choice = "Unsorted"
        else:
            folder_choice = str(ai_data.get("folder", "Unsorted")).strip()

        collection_id = get_or_create_collection(folder_choice) if folder_choice != "Unsorted" else -1

        # --- Tags Priority Logic ---
        ai_tags = ai_data.get("tags", ["telegram"])
        if not isinstance(ai_tags, list): ai_tags = ["telegram"]
        ai_tags = [str(tag).strip().lower() for tag in ai_tags if str(tag).strip()]

        if manual_tags and no_tags:
            # Both provided: ONLY use manual tags, IGNORE AI tags completely
            final_tags = manual_tags
        elif no_tags:
            final_tags = []
        elif manual_tags:
            final_tags = list(dict.fromkeys(manual_tags + ai_tags))
        else:
            final_tags = ai_tags

        # --- Link Priority Logic ---
        if explicit_link_given:
            target_url = url
        elif no_link:
            target_url = "https://telegram.org"
        else:
            target_url = url or "https://telegram.org"

        success = save_to_raindrop(target_url, title, excerpt, note, final_tags, collection_id)

        if success:
            msg = f"✅ *Saved to Raindrop!*\n\n📌 *Title:* {title}\n📁 *Folder:* {folder_choice}"
            if excerpt:
                msg += f"\n📝 *Excerpt:* {excerpt}"
            if note:
                msg += f"\n📓 *Notes:*\n{note}"
            if final_tags:
                msg += f"\n🏷️ *Tags:* {', '.join(final_tags)}"
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
                    reply_telegram(chat_id, "ℹ️ Usage: `/search <keyword or #tag> [/result <1-50>]`")
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