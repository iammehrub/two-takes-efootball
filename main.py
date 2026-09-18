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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()

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
    """Generate the caption with transient-error retries and model fallback."""
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

    models = []
    for model in (
        GEMINI_MODEL,
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
    ):
        if model and model not in models:
            models.append(model)

    last_error = ""
    for model in models:
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 280,
            },
        }

        for attempt in range(4):
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=50,
            )

            if response.ok:
                data = response.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    last_error = f"{model}: no candidates returned: {data}"
                    break

                parts = candidates[0].get("content", {}).get("parts", [])
                generated = "\n".join(
                    p.get("text", "") for p in parts
                ).strip()

                if generated:
                    print(f"Gemini caption generated with {model}.")
                    return generated

                last_error = f"{model}: empty response: {data}"
                break

            last_error = (
                f"{model}: HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

            # 429/5xx are transient candidates. Retry with exponential backoff.
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    delay = min(20, 2 ** attempt) + random.random()
                    print(
                        f"Gemini {model} returned {response.status_code}; "
                        f"retrying in {delay:.1f}s..."
                    )
                    import time
                    time.sleep(delay)
                    continue

            # 400/401/403/404 and other client errors should move to fallback.
            break

        print(f"Trying next Gemini model after failure: {last_error}")

    fail(f"All Gemini models failed. Last error: {last_error}")

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


def resolve_page_access_token(token: str) -> str:
    """Resolve a usable Page Access Token and, when needed, auto-detect the Page."""
    global FACEBOOK_PAGE_ID

    me = requests.get(
        "https://graph.facebook.com/me",
        params={"fields": "id,name", "access_token": token},
        headers={"User-Agent": UA},
        timeout=30,
    )

    if me.ok:
        me_data = me.json()
        print(
            f"Facebook token identity: "
            f"{me_data.get('name', 'unknown')} ({me_data.get('id', 'unknown')})"
        )

        # Already a Page token for the configured Page.
        if str(me_data.get("id", "")) == str(FACEBOOK_PAGE_ID):
            print("Configured FACEBOOK_PAGE_ID matches the Page token identity.")
            return token

    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={
            "fields": "id,name,access_token,tasks",
            "access_token": token,
        },
        headers={"User-Agent": UA},
        timeout=30,
    )

    if not response.ok:
        fail(
            "Facebook token cannot list Pages. "
            f"/me/accounts returned HTTP {response.status_code}: "
            f"{response.text[:700]}"
        )

    pages = response.json().get("data", [])
    if not pages:
        fail(
            "This Facebook token has access to no Pages through /me/accounts. "
            "Generate the token from the Facebook account that has access to "
            "the target Page and grant the required Page permissions."
        )

    # Exact configured-page match.
    for page in pages:
        page_id = str(page.get("id", ""))
        if page_id == str(FACEBOOK_PAGE_ID):
            page_token = (page.get("access_token") or "").strip()
            if page_token:
                print(
                    f"Found configured Page: {page.get('name', 'unknown')} "
                    f"({page_id})."
                )
                return page_token

    # This token currently exposes exactly one Page, so safely use it.
    # This avoids breaking the workflow when the Page-ID secret was entered
    # incorrectly but the token has access to only one Page.
    if len(pages) == 1:
        page = pages[0]
        page_id = str(page.get("id", ""))
        page_token = (page.get("access_token") or "").strip()
        page_name = page.get("name", "unknown")

        if page_id and page_token:
            print(
                f"Configured Page ID '{FACEBOOK_PAGE_ID}' did not match. "
                f"Using the only Page visible to this token: "
                f"{page_name} ({page_id})."
            )
            FACEBOOK_PAGE_ID = page_id
            return page_token

    visible = [
        f"{p.get('name', 'unknown')} ({p.get('id', 'unknown')})"
        for p in pages
    ]
    fail(
        "FACEBOOK_PAGE_ID does not match any Page visible to the token, and "
        "the token can access multiple Pages. Visible Pages: "
        + "; ".join(visible)
        + ". Set FACEBOOK_PAGE_ID to the target Page's numeric ID."
    )

def publish_photo(caption: str, image_bytes: bytes, access_token: str) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/photos",
        data={
            "message": caption,
            "published": "true",
            "access_token": access_token,
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


def publish_text(caption: str, access_token: str) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/feed",
        data={
            "message": caption,
            "access_token": access_token,
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
    page_access_token = resolve_page_access_token(FACEBOOK_PAGE_ACCESS_TOKEN)

    image_bytes, image_url = search_pexels(config["pexels"])

    if image_bytes:
        result = publish_photo(caption, image_bytes, page_access_token)
        post_type = "photo"
    else:
        result = publish_text(caption, page_access_token)
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
