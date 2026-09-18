import json
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape
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
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free").strip()
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


def fetch_google_news(query: str, limit: int = 12) -> list[dict]:
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


def is_efootball_story(story: dict) -> bool:
    """Reject generic football stories that are not actually about eFootball."""
    text = " ".join(
        [
            story.get("title", ""),
            story.get("description", ""),
            story.get("source", ""),
        ]
    ).lower()

    required_signals = (
        "efootball",
        "e-football",
        "e football",
        "konami",
        "dream team",
        "special player list",
        "epic player",
        "booster player",
        "efootball league",
        "efootball points",
        "efootball coins",
        "master league",
        "pes",
    )
    return any(signal in text for signal in required_signals)


def choose_story(items: list[dict], state: dict) -> dict:
    posted_links = {x.get("link") for x in state.get("posted", [])}

    relevant = [
        item for item in items
        if item.get("link") not in posted_links
        and is_efootball_story(item)
    ]

    if relevant:
        return relevant[0]

    # Do not publish generic football content just to fill the schedule.
    relevant_old = [
        item for item in items
        if is_efootball_story(item)
    ]

    if relevant_old:
        return relevant_old[0]

    fail(
        "No eFootball-specific story was found. "
        "The bot will not publish generic football content."
    )

def choose_story(items: list[dict], state: dict) -> dict:
    posted_links = {x.get("link") for x in state.get("posted", [])}
    fresh = [x for x in items if x["link"] not in posted_links]
    if fresh:
        return fresh[0]
    if not items:
        fail("No news items were returned.")
    return items[0]


def call_ai(story: dict, slot_name: str) -> str:
    """Generate a caption using OpenRouter's free-model router."""
    prompt = f"""
You write social posts for an eFootball Facebook page called "Two Takes EFootball".

Content type: {slot_name}

Use ONLY facts supported by the supplied source. Do not invent release dates,
player ratings, event details, pack contents, odds, or Konami statements.

Write a Facebook post for an English + Banglish audience.
Requirements:
- 50 to 90 words.
- First line must be the post title.
- Strong, specific eFootball headline.
- Natural English/Banglish mix.
- Mention eFootball clearly.
- Add 2-4 relevant emojis.
- End with ONE simple engagement question.
- Add 4-7 relevant hashtags.
- The output MUST use exactly this structure:\nTITLE: <short headline>\n\n<caption body>\n\nTAGS: <hashtags>\n- The title, body, and tags must all be specifically about eFootball.
- Do not claim rumors are confirmed.
- Do not mention that you are an AI.

SOURCE:
Title: {story["title"]}
Publisher: {story["source"]}
Date: {story["pub_date"]}
Summary: {story["description"]}
URL: {story["link"]}
""".strip()

    if not OPENROUTER_API_KEY:
        fail("Missing GitHub Actions secret: OPENROUTER_API_KEY")

    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/iammehrub/two-takes-efootball",
        "X-Title": "Two Takes EFootball",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": 220,
    }

    import time
    last_error = ""

    for attempt in range(5):
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=(10, 35),
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 4:
                delay = min(15, 2 ** attempt) + random.random()
                print(
                    f"OpenRouter network failure; retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            break

        if response.ok:
            try:
                data = response.json()
            except ValueError:
                fail("OpenRouter returned invalid JSON.")

            choices = data.get("choices", [])
            if not choices:
                fail(f"OpenRouter returned no choices: {data}")

            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict)
                )

            generated = str(content).strip()
            if generated:
                print(
                    f"Caption generated with OpenRouter model {OPENROUTER_MODEL}."
                )
                return generated

            fail(f"OpenRouter returned empty content: {data}")

        last_error = f"HTTP {response.status_code}: {response.text[:700]}"

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 4:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        float(retry_after)
                        if retry_after
                        else min(15, 2 ** attempt)
                    )
                except ValueError:
                    delay = min(15, 2 ** attempt)

                delay += random.random()
                print(
                    f"OpenRouter returned {response.status_code}; "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue

        break

    fail(f"OpenRouter failed after retries. Last error: {last_error}")



def extract_og_image(article_url: str) -> tuple[bytes, str]:
    """Try to use the article's own Open Graph image first."""
    try:
        response = requests.get(
            article_url,
            headers={"User-Agent": UA},
            timeout=(10, 20),
            allow_redirects=True,
        )
        if not response.ok:
            return b"", ""

        html = response.text[:2_000_000]
        patterns = [
            r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image[\"']",
            r"<meta[^>]+name=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']twitter:image[\"']",
        ]

        image_url = ""
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                image_url = unescape(match.group(1)).strip()
                break

        if not image_url:
            return b"", ""

        image = requests.get(
            image_url,
            headers={"User-Agent": UA},
            timeout=(10, 25),
        )
        if image.ok and image.content:
            content_type = image.headers.get("Content-Type", "")
            if content_type.startswith("image/"):
                return image.content, image_url

    except (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ):
        pass
    except Exception as exc:
        print(f"Article image extraction failed: {exc}")

    return b"", ""



def search_pexels(query: str) -> tuple[bytes, str]:
    response = requests.get(
        "https://api.pexels.com/v1/search",
        params={
            "query": query,
            "per_page": 12,
            "orientation": "landscape",
        },
        headers={
            "Authorization": PEXELS_API_KEY,
            "User-Agent": UA,
        },
        timeout=(10, 25),
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

        try:
            image = requests.get(
                image_url,
                headers={"User-Agent": UA},
                timeout=(10, 25),
            )
            if image.ok and image.content:
                return image.content, image_url
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ):
            continue

    return b"", ""

def resolve_page_access_token(token: str) -> str:
    """Resolve the Page Access Token for FACEBOOK_PAGE_ID."""
    global FACEBOOK_PAGE_ID

    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={
            "fields": "id,name,access_token,tasks",
            "access_token": token,
        },
        headers={"User-Agent": UA},
        timeout=(10, 30),
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
            "This Facebook token has access to no Pages. "
            "Generate a User Access Token from the Facebook account that "
            "has access to the Page, then derive a Page Access Token."
        )

    # Exact configured Page match.
    for page in pages:
        page_id = str(page.get("id", ""))
        if page_id == str(FACEBOOK_PAGE_ID):
            page_token = (page.get("access_token") or "").strip()
            if not page_token:
                fail(
                    f"Meta found {page.get('name', 'the Page')} ({page_id}) "
                    "but did not return a Page Access Token."
                )

            tasks = page.get("tasks") or []
            print(
                f"Found configured Page: {page.get('name', 'unknown')} "
                f"({page_id})."
            )
            print(
                "Page tasks: "
                + (", ".join(tasks) if tasks else "not returned")
            )
            return page_token

    # Safe auto-detection when the token can access exactly one Page.
    if len(pages) == 1:
        page = pages[0]
        page_id = str(page.get("id", ""))
        page_token = (page.get("access_token") or "").strip()
        page_name = page.get("name", "unknown")
        tasks = page.get("tasks") or []

        if not page_token:
            fail(
                f"Meta found {page_name} ({page_id}) but did not return "
                "a Page Access Token."
            )

        print(
            f"Configured Page ID did not match. Using the only Page visible "
            f"to this token: {page_name} ({page_id})."
        )
        print(
            "Page tasks: "
            + (", ".join(tasks) if tasks else "not returned")
        )

        FACEBOOK_PAGE_ID = page_id
        return page_token

    visible = [
        f"{p.get('name', 'unknown')} ({p.get('id', 'unknown')})"
        for p in pages
    ]
    fail(
        "FACEBOOK_PAGE_ID does not match a Page visible to the token. "
        "Visible Pages: " + "; ".join(visible)
    )


def verify_page_publishing_access(access_token: str) -> None:
    """Validate only the Page identity; task data comes from /me/accounts."""
    response = requests.get(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}",
        params={
            "fields": "id,name",
            "access_token": access_token,
        },
        headers={"User-Agent": UA},
        timeout=30,
    )

    if not response.ok:
        fail(
            "The resolved Page Access Token cannot read the target Page. "
            f"HTTP {response.status_code}: {response.text[:700]}"
        )

    data = response.json()
    page_id = str(data.get("id", ""))
    page_name = data.get("name", "unknown")

    print(f"Page token identity check: {page_name} ({page_id})")

    if page_id != str(FACEBOOK_PAGE_ID):
        fail(
            f"Resolved Page Access Token belongs to {page_id}, "
            f"expected {FACEBOOK_PAGE_ID}."
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
    if not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if not PEXELS_API_KEY:
        missing.append("PEXELS_API_KEY")

    if missing:
        fail("Missing GitHub Actions secrets: " + ", ".join(missing))

    if SLOT not in SLOT_CONFIG:
        fail("POST_SLOT must be 1, 2, 3, or 4.")

    config = SLOT_CONFIG[SLOT]
    state = load_state()

    stories = fetch_google_news(config["query"] + " eFootball")
    story = choose_story(stories, state)
    caption = call_ai(story, config["name"])
    page_access_token = resolve_page_access_token(FACEBOOK_PAGE_ACCESS_TOKEN)
    verify_page_publishing_access(page_access_token)

    image_bytes, image_url = extract_og_image(story["link"])
    if not image_bytes:
        image_bytes, image_url = search_pexels("eFootball " + config["pexels"])

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
