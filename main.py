import json
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
from PIL import Image, ImageDraw, ImageFont

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
    1: {
        "name": "eFootball News",
        "query": '"eFootball" "KONAMI" latest news when:3d',
        "pexels": "eFootball gaming",
    },
    2: {
        "name": "Updates & Events",
        "query": '"eFootball" update event campaign KONAMI when:7d',
        "pexels": "football video game gaming",
    },
    3: {
        "name": "Player Ratings",
        "query": '"eFootball" "Live Update" ratings players when:7d',
        "pexels": "football video game player",
    },
    4: {
        "name": "Tips & Community",
        "query": '"eFootball" tips tactics Dream Team when:7d',
        "pexels": "football video game tactics",
    },
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


def parse_news_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_google_news(query: str, limit: int = 20) -> list[dict]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    response = requests.get(url, headers={"User-Agent": UA}, timeout=(10, 20))
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)

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

        published_dt = parse_news_datetime(pub_date)

        # Hard freshness gate. We do not post old stories just to fill a slot.
        if published_dt is None or published_dt < cutoff:
            continue

        if title and link:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "pub_date": pub_date,
                    "published_dt": published_dt,
                    "source": source,
                }
            )

    # Newest first.
    items.sort(
        key=lambda item: item.get("published_dt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
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
        # Prefer official KONAMI sources when freshness is comparable.
        official = [
            item for item in relevant
            if "KONAMI" in item.get("source", "").upper()
            or "KONAMI" in item.get("title", "").upper()
        ]
        return (official or relevant)[0]

    fail(
        "No fresh eFootball-specific story was found in the last 7 days. "
        "The bot will not publish stale or generic football content."
    )



def _clean_ai_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(
            p.get("text", "")
            for p in value
            if isinstance(p, dict) and p.get("text")
        ).strip()
    return str(value).strip()


def parse_generated_post(raw: str, story: dict, slot_name: str) -> dict:
    raw = _clean_ai_text(raw)

    title = ""
    tags = ""
    body = ""

    if raw and raw.lower() not in {"none", "null"}:
        title_match = re.search(r"(?im)^TITLE:\s*(.+?)\s*$", raw)
        tags_match = re.search(r"(?im)^TAGS:\s*(.+?)\s*$", raw)

        if title_match:
            title = title_match.group(1).strip()

        body_start = title_match.end() if title_match else 0
        body_end = tags_match.start() if tags_match else len(raw)
        body = raw[body_start:body_end].strip()

        if tags_match:
            tags = tags_match.group(1).strip()

    # Deterministic fallback: use verified source facts rather than inventing.
    if not title:
        title = story["title"].split(" - ")[0].strip()

    if "efootball" not in title.lower():
        title = "eFootball: " + title

    body = re.sub(r"(?im)^TITLE:\s*.*$", "", body).strip()
    body = re.sub(r"(?im)^TAGS:\s*.*$", "", body).strip()

    if not body:
        summary = story.get("description", "").strip()
        body = summary[:650].rstrip() if summary else (
            "A new eFootball update has been reported. "
            "Check the official details before making any changes to your team."
        )

    hashtag_tokens = re.findall(r"#[A-Za-z0-9_]+", tags)
    if not hashtag_tokens:
        hashtag_tokens = [
            "#eFootball",
            "#eFootball2026",
            "#KONAMI",
            "#eFootballNews",
            "#DreamTeam",
        ]

    tags = " ".join(dict.fromkeys(hashtag_tokens[:7]))

    return {
        "title": title,
        "body": body,
        "tags": tags,
        "caption": f"{title}\n\n{body}\n\n{tags}",
    }



def call_ai(story: dict, slot_name: str) -> dict:
    """Generate and validate title/body/tags through OpenRouter."""
    prompt = f"""
You write posts for the Facebook page "Two Takes EFootball".

This is an eFootball-specific news post.

Content type: {slot_name}

STRICT RULES:
- Use ONLY facts present in the supplied source.
- Never invent player ratings, events, dates, rewards, pack contents, or quotes.
- Do not discuss real-world football unless it is directly part of the eFootball source.
- The title MUST be specifically about eFootball.
- Output EXACTLY:
TITLE: <short eFootball headline>

<body, 45-80 words>

TAGS: <4-7 hashtags>

Source title: {story["title"]}
Source: {story["source"]}
Published: {story["pub_date"]}
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
        "temperature": 0.35,
        "max_tokens": 320,
        "reasoning": {"exclude": True},
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
                last_error = f"OpenRouter returned no choices: {data}"
                break

            message = choices[0].get("message") or {}
            content = _clean_ai_text(message.get("content"))

            if not content:
                content = _clean_ai_text(choices[0].get("text"))

            parsed = parse_generated_post(content, story, slot_name)
            if content:
                print(
                    f"Caption generated with OpenRouter model "
                    f"{OPENROUTER_MODEL}."
                )
                return parsed

            print(
                "OpenRouter returned no final text; using deterministic "
                "source-based eFootball caption."
            )
            return parsed

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

    # Deterministic fallback: never publish "None".
    fallback = parse_generated_post(
        "",
        story,
        slot_name,
    )
    if fallback:
        print("OpenRouter failed; using deterministic source-based fallback caption.")
        return fallback

    fail(f"OpenRouter failed after retries. Last error: {last_error}")



def _load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = word if not current else current + " " + word
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def create_branded_image(title: str, source: str, published: str) -> bytes:
    """Create a clean eFootball news card instead of using random/Google images."""
    width, height = 1200, 675

    image = Image.new("RGB", (width, height), (8, 24, 18))
    draw = ImageDraw.Draw(image)

    # Dynamic football-pitch background.
    draw.rectangle((0, 390, width, height), fill=(10, 65, 38))
    for x in range(-200, width + 200, 120):
        draw.line((x, height, x + 260, 390), fill=(18, 94, 53), width=3)
    for y in range(420, height, 55):
        draw.line((0, y, width, y), fill=(18, 94, 53), width=2)

    # Large abstract ball.
    ball_x, ball_y, radius = 980, 135, 150
    draw.ellipse(
        (ball_x - radius, ball_y - radius, ball_x + radius, ball_y + radius),
        outline=(230, 245, 238),
        width=4,
    )
    for angle in range(0, 360, 72):
        import math
        px = ball_x + int(radius * 0.72 * math.cos(math.radians(angle)))
        py = ball_y + int(radius * 0.72 * math.sin(math.radians(angle)))
        draw.line((ball_x, ball_y, px, py), fill=(230, 245, 238), width=3)

    white = (245, 249, 247)
    accent = (50, 220, 150)
    muted = (185, 205, 195)

    brand_font = _load_font(34, True)
    label_font = _load_font(23, True)
    title_font = _load_font(54, True)
    small_font = _load_font(21, False)

    draw.text(
        (70, 48),
        "TWO TAKES EFOOTBALL",
        font=brand_font,
        fill=white,
    )
    draw.text(
        (70, 105),
        "NEWS",
        font=label_font,
        fill=accent,
    )

    max_width = 780
    title_lines = wrap_text(draw, title, title_font, max_width)

    y = 170
    for line in title_lines[:4]:
        draw.text((70, y), line, font=title_font, fill=white)
        y += 64

    draw.text(
        (70, 560),
        f"{source or 'eFootball News'}  •  {published[:16]}",
        font=small_font,
        fill=muted,
    )
    draw.text(
        (70, 610),
        "eFootball updates • news • ratings • events",
        font=label_font,
        fill=white,
    )

    out = WORK_PATH / "efootball_news_card.jpg"
    image.save(out, "JPEG", quality=92, optimize=True)
    return out.read_bytes()



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
    caption_data = call_ai(story, config["name"])
    if caption_data["caption"].strip().lower() == "none":
        fail("Generated caption evaluated to None.")
    if "efootball" not in caption_data["caption"].lower():
        fail("Generated caption is not explicitly eFootball-specific.")
    page_access_token = resolve_page_access_token(FACEBOOK_PAGE_ACCESS_TOKEN)
    verify_page_publishing_access(page_access_token)

    image_bytes = create_branded_image(
        caption_data["title"],
        story["source"],
        story["pub_date"],
    )
    image_url = "generated://two-takes-efootball-news-card"

    result = publish_photo(
        caption_data["caption"],
        image_bytes,
        page_access_token,
    )
    post_type = "photo"

    entry = {
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "slot": SLOT,
        "type": post_type,
        "story_title": story["title"],
        "source": story["source"],
        "link": story["link"],
        "facebook_id": result.get("post_id") or result.get("id"),
        "image_url": image_url,
        "caption": caption,
    }


    # Remove the two legacy posts created by the broken version of this bot.
    # Failures are logged but do not block the corrected post.
    legacy_post_ids = [
        "1397515220100650_122095343775487467",
        "1397515220100650_122095346487487467",
    ]
    for legacy_id in legacy_post_ids:
        try:
            delete_response = requests.delete(
                f"https://graph.facebook.com/{legacy_id}",
                params={"access_token": page_access_token},
                timeout=(10, 30),
            )
            if delete_response.ok:
                print(f"Removed legacy Facebook post {legacy_id}.")
            else:
                print(
                    f"Could not remove legacy Facebook post {legacy_id}: "
                    f"HTTP {delete_response.status_code}"
                )
        except requests.RequestException as exc:
            print(f"Legacy post cleanup failed for {legacy_id}: {exc}")

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
