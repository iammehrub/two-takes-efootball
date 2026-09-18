import json
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "state.json"
WORK_PATH = ROOT / "work"
WORK_PATH.mkdir(exist_ok=True)
STATE_PATH.parent.mkdir(exist_ok=True)

FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GEMINI", "").strip()
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip() or os.environ.get("PEXELS", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

try:
    SLOT = int(os.environ.get("POST_SLOT", "1"))
except ValueError:
    SLOT = 0

SLOT_CONFIG = {
    1: {"name": "eFootball News", "query": "eFootball latest news Konami", "pexels": "football video game"},
    2: {"name": "Updates & Events", "query": "eFootball update event campaign Konami", "pexels": "football stadium"},
    3: {"name": "Player Ratings", "query": "eFootball player ratings featured players", "pexels": "football player"},
    4: {"name": "Tips & Community", "query": "eFootball tips formation tactics players", "pexels": "football tactics"},
}

UA = "TwoTakesEFootballBot/1.0 (+GitHub Actions)"


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"posted": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("posted"), list):
            return {"posted": []}
        return data
    except Exception:
        return {"posted": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_google_news(query: str, limit: int = 8) -> list[dict]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    response = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = strip_html(item.findtext("description") or "")
        pub_date = (item.findtext("pubDate") or "").strip()
        source_node = item.find("source")
        source = (
            source_node.text.strip()
            if source_node is not None and source_node.text
            else ""
        )
        if title and link:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "pub_date": pub_date,
                    "source": source,
                }
            )
    return items


def choose_story(items: list[dict], state: dict) -> dict:
    posted_links = {x.get("link") for x in state.get("posted", [])}
    fresh = [x for x in items if x["link"] not in posted_links]
    if fresh:
        return fresh[0]
    if not items:
        fail("No news items were returned.")
    return items[0]


def call_gemini(story: dict, slot_name: str) -> str:
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = f"""
You write social posts for an eFootball Facebook page called "Two Takes EFootball".

Content type: {slot_name}

Use ONLY facts supported by the supplied source. Do not invent release dates,
player ratings, event details, pack contents, odds, or Konami statements.

Write a Facebook post for an English + Banglish audience.
Requirements:
- 60 to 110 words.
- Strong first line.
- Natural English/Banglish mix, not every sentence mixed.
- Mention eFootball clearly.
- Add 2-4 relevant emojis.
- End with ONE simple engagement question.
- Add 3-6 relevant hashtags.
- Do not use markdown headings.
- Do not claim rumors are confirmed.
- Do not mention that you are an AI.

SOURCE:
Title: {story["title"]}
Publisher: {story["source"]}
Date: {story["pub_date"]}
Summary: {story["description"]}
URL: {story["link"]}
""".strip()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 280,
        },
    }

    response = requests.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=40,
    )
    if response.status_code >= 400:
        fail(f"Gemini API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        fail(f"Gemini returned no candidates: {data}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts).strip()
    if not text:
        fail(f"Gemini returned no text: {data}")
    return text


def search_pexels(query: str) -> tuple[bytes, str]:
    response = requests.get(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": 12, "orientation": "landscape"},
        headers={"Authorization": PEXELS_API_KEY, "User-Agent": UA},
        timeout=25,
    )
    response.raise_for_status()

    photos = response.json().get("photos", [])
    if not photos:
        return b"", ""

    random.shuffle(photos)
    for photo in photos:
        src = photo.get("src", {})
        image_url = (
            src.get("large2x")
            or src.get("large")
            or src.get("original")
        )
        if not image_url:
            continue

        image = requests.get(
            image_url,
            headers={"User-Agent": UA},
            timeout=30,
        )
        if image.ok and image.content:
            return image.content, image_url

    return b"", ""


def publish_photo(caption: str, image_bytes: bytes) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/photos",
        data={
            "message": caption,
            "published": "true",
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        },
        files={
            "source": (
                "two-takes-efootball.jpg",
                image_bytes,
                "image/jpeg",
            )
        },
        timeout=60,
    )

    if response.status_code >= 400:
        fail(
            f"Facebook API error {response.status_code}: "
            f"{response.text[:700]}"
        )

    result = response.json()
    if "id" not in result and "post_id" not in result:
        fail(f"Facebook returned an unexpected response: {result}")
    return result


def publish_text(caption: str) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/feed",
        data={
            "message": caption,
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )

    if response.status_code >= 400:
        fail(
            f"Facebook feed API error {response.status_code}: "
            f"{response.text[:700]}"
        )

    result = response.json()
    if "id" not in result:
        fail(f"Facebook returned an unexpected response: {result}")
    return result


def main() -> None:
    missing = []

    if not FACEBOOK_PAGE_ID:
        missing.append("FACEBOOK_PAGE_ID")
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        missing.append("FACEBOOK_PAGE_ACCESS_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not PEXELS_API_KEY:
        missing.append("PEXELS_API_KEY")

    if missing:
        fail("Missing GitHub Actions secrets: " + ", ".join(missing))

    if SLOT not in SLOT_CONFIG:
        fail("POST_SLOT must be 1, 2, 3, or 4.")

    config = SLOT_CONFIG[SLOT]
    state = load_state()

    stories = fetch_google_news(config["query"])
    story = choose_story(stories, state)
    caption = call_gemini(story, config["name"])

    image_bytes, image_url = search_pexels(config["pexels"])

    if image_bytes:
        result = publish_photo(caption, image_bytes)
        post_type = "photo"
    else:
        result = publish_text(caption)
        post_type = "text"

    entry = {
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "slot": SLOT,
        "type": post_type,
        "story_title": story["title"],
        "source": story["source"],
        "link": story["link"],
        "facebook_id": result.get("post_id") or result.get("id"),
        "image_url": image_url,
    }

    state.setdefault("posted", []).append(entry)
    state["posted"] = state["posted"][-100:]
    save_state(state)

    print(json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
