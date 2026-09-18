import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "state.json"
WORK_PATH = ROOT / "work"

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
WORK_PATH.mkdir(parents=True, exist_ok=True)

FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get(
    "FACEBOOK_PAGE_ACCESS_TOKEN", ""
).strip()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "openrouter/free"
).strip()

try:
    SLOT = int(os.environ.get("POST_SLOT", "1"))
except ValueError:
    SLOT = 0

USER_AGENT = "TwoTakesEFootballBot/2.0 (+GitHub Actions)"

SLOT_CONFIG = {
    1: {
        "name": "eFootball News",
        "queries": [
            'site:konami.com/efootball/en/topic/news eFootball when:3d',
            '"eFootball" "KONAMI" when:3d',
        ],
    },
    2: {
        "name": "Updates & Events",
        "queries": [
            'site:konami.com/efootball/en/topic/news eFootball update event when:3d',
            '"eFootball" update event campaign KONAMI when:3d',
        ],
    },
    3: {
        "name": "Player Ratings",
        "queries": [
            'site:konami.com/efootball/en/topic/news eFootball ratings players when:3d',
            '"eFootball" "Live Update" ratings players when:3d',
        ],
    },
    4: {
        "name": "Tips & Community",
        "queries": [
            '"eFootball" "Dream Team" tactics players when:3d',
            '"eFootball" tips formations community when:3d',
        ],
    },
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"posted": []}

    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"posted": []}

    if not isinstance(data, dict) or not isinstance(data.get("posted"), list):
        return {"posted": []}

    return data


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_google_news(query: str, limit: int = 20) -> list[dict]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )

    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=(10, 20),
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
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

        published_dt = parse_news_datetime(pub_date)

        if not title or not link or published_dt is None:
            continue

        if published_dt < cutoff:
            continue

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

    items.sort(
        key=lambda x: x["published_dt"],
        reverse=True,
    )
    return items


def is_efootball_story(story: dict) -> bool:
    text = " ".join(
        [
            story.get("title", ""),
            story.get("description", ""),
            story.get("source", ""),
        ]
    ).lower()

    signals = (
        "efootball",
        "e-football",
        "e football",
        "konami",
        "dream team",
        "efootball league",
        "efootball points",
        "efootball coins",
        "booster player",
        "epic player",
        "special player list",
        "live update",
        "pes",
    )

    return any(signal in text for signal in signals)


def choose_story(items: list[dict], state: dict) -> dict:
    posted_links = {entry.get("link") for entry in state.get("posted", [])}

    relevant = [
        item
        for item in items
        if item.get("link") not in posted_links
        and is_efootball_story(item)
    ]

    if not relevant:
        fail(
            "No fresh eFootball-specific story was found in the last 3 days. "
            "The bot will not publish stale or generic football content."
        )

    official = [
        item
        for item in relevant
        if "KONAMI" in item.get("source", "").upper()
        or "KONAMI" in item.get("title", "").upper()
    ]

    chosen = (official or relevant)[0]

    print(
        "Selected story: "
        f"{chosen['title']} | {chosen['source']} | {chosen['pub_date']}"
    )
    return chosen


def _clean_ai_text(value) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        return "\n".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and part.get("text")
        ).strip()

    return str(value).strip()


def deterministic_post(story: dict) -> dict:
    title = story["title"].split(" - ")[0].strip()

    if "efootball" not in title.lower():
        title = "eFootball: " + title

    summary = story.get("description", "").strip()

    if not summary:
        summary = (
            "KONAMI has published a new eFootball update. "
            "Check the official details for the latest information."
        )

    body = summary[:650].rstrip()

    tags = (
        "#eFootball #eFootball2026 #KONAMI "
        "#eFootballNews #DreamTeam"
    )

    return {
        "title": title[:120],
        "body": body,
        "tags": tags,
        "caption": f"{title[:120]}\n\n{body}\n\n{tags}",
    }


def parse_generated_post(raw: str, story: dict) -> dict:
    raw = _clean_ai_text(raw)

    if not raw or raw.lower() in {"none", "null"}:
        return deterministic_post(story)

    title_match = re.search(
        r"(?im)^TITLE:\s*(.+?)\s*$",
        raw,
    )
    tags_match = re.search(
        r"(?im)^TAGS:\s*(.+?)\s*$",
        raw,
    )

    title = title_match.group(1).strip() if title_match else ""
    tags = tags_match.group(1).strip() if tags_match else ""

    body_start = title_match.end() if title_match else 0
    body_end = tags_match.start() if tags_match else len(raw)
    body = raw[body_start:body_end].strip()

    body = re.sub(
        r"(?im)^TITLE:\s*.*$",
        "",
        body,
    ).strip()

    body = re.sub(
        r"(?im)^TAGS:\s*.*$",
        "",
        body,
    ).strip()

    if not title:
        title = story["title"].split(" - ")[0].strip()

    if "efootball" not in title.lower():
        title = "eFootball: " + title

    if not body:
        body = story.get("description", "").strip()

    if not body:
        body = deterministic_post(story)["body"]

    hashtags = re.findall(r"#[A-Za-z0-9_]+", tags)

    if not hashtags:
        hashtags = [
            "#eFootball",
            "#eFootball2026",
            "#KONAMI",
            "#eFootballNews",
            "#DreamTeam",
        ]

    tags = " ".join(dict.fromkeys(hashtags[:7]))

    return {
        "title": title[:120],
        "body": body[:900].strip(),
        "tags": tags,
        "caption": (
            f"{title[:120]}\n\n"
            f"{body[:900].strip()}\n\n"
            f"{tags}"
        ),
    }


def call_ai(story: dict, slot_name: str) -> dict:
    if not OPENROUTER_API_KEY:
        fail("Missing GitHub Actions secret: OPENROUTER_API_KEY")

    prompt = f"""
You create Facebook posts for "Two Takes EFootball".

Content type: {slot_name}

Use ONLY information supported by the supplied source.
Do not invent dates, player ratings, rewards, events, packs, odds, or quotes.
Do not turn real-world football news into eFootball news.

Write:
TITLE: one short, specific eFootball headline

<body of 45-80 words>

TAGS: 4-7 relevant hashtags

The title MUST mention eFootball or an unmistakably eFootball-specific feature,
player, update, event, or issue.

SOURCE TITLE:
{story["title"]}

SOURCE:
{story["source"]}

PUBLISHED:
{story["pub_date"]}

SUMMARY:
{story["description"]}

URL:
{story["link"]}
""".strip()

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.3,
        "max_tokens": 320,
        "reasoning": {
            "exclude": True,
        },
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/iammehrub/two-takes-efootball",
        "X-Title": "Two Takes EFootball",
    }

    last_error = ""

    for attempt in range(4):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=(10, 35),
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

            if attempt < 3:
                delay = min(12, 2 ** attempt) + random.random()
                print(
                    f"OpenRouter network error; retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue

            break

        if response.ok:
            try:
                data = response.json()
            except ValueError:
                last_error = "OpenRouter returned invalid JSON."
                break

            choices = data.get("choices") or []

            if choices:
                message = choices[0].get("message") or {}
                content = _clean_ai_text(message.get("content"))

                if not content:
                    content = _clean_ai_text(
                        choices[0].get("text")
                    )

                parsed = parse_generated_post(content, story)

                if content:
                    print(
                        "Caption generated with OpenRouter model "
                        f"{OPENROUTER_MODEL}."
                    )
                else:
                    print(
                        "OpenRouter returned no final text; using a "
                        "source-based fallback caption."
                    )

                return parsed

            last_error = f"OpenRouter returned no choices: {data}"
            break

        last_error = (
            f"HTTP {response.status_code}: "
            f"{response.text[:700]}"
        )

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 3:
                delay = min(12, 2 ** attempt) + random.random()
                print(
                    f"OpenRouter returned {response.status_code}; "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue

        break

    print(
        "OpenRouter failed; using a deterministic source-based caption. "
        f"Last error: {last_error}"
    )
    return deterministic_post(story)


def resolve_page_access_token(token: str) -> str:
    """Resolve a Page Access Token from the supplied Facebook token."""
    if not token:
        fail("Missing FACEBOOK_PAGE_ACCESS_TOKEN.")

    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={
            "fields": "id,name,access_token,tasks",
            "access_token": token,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=(10, 30),
    )

    if not response.ok:
        fail(
            "Facebook token cannot list Pages. "
            f"HTTP {response.status_code}: {response.text[:700]}"
        )

    pages = response.json().get("data", [])

    if not pages:
        fail(
            "This Facebook token has access to no Pages. "
            "Use a token from the Facebook account that manages the Page."
        )

    for page in pages:
        page_id = str(page.get("id", ""))

        if page_id != str(FACEBOOK_PAGE_ID):
            continue

        page_token = (page.get("access_token") or "").strip()
        if not page_token:
            fail(
                f"Meta found the Page {page.get('name', 'unknown')} "
                f"({page_id}) but returned no Page Access Token."
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

    if len(pages) == 1:
        page = pages[0]
        page_id = str(page.get("id", ""))
        page_token = (page.get("access_token") or "").strip()
        page_name = page.get("name", "unknown")

        if not page_token:
            fail(
                f"Meta found {page_name} ({page_id}) but returned no "
                "Page Access Token."
            )

        print(
            f"Configured Page ID did not match. Using the only Page visible "
            f"to this token: {page_name} ({page_id})."
        )

        FACEBOOK_PAGE_ID = page_id
        return page_token

    visible = [
        f"{page.get('name', 'unknown')} ({page.get('id', 'unknown')})"
        for page in pages
    ]

    fail(
        "FACEBOOK_PAGE_ID does not match a Page visible to the token. "
        "Visible Pages: " + "; ".join(visible)
    )


def verify_page_access(page_access_token: str) -> None:
    response = requests.get(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}",
        params={
            "fields": "id,name",
            "access_token": page_access_token,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=(10, 30),
    )

    if not response.ok:
        fail(
            "The resolved Page Access Token cannot read the target Page. "
            f"HTTP {response.status_code}: {response.text[:700]}"
        )

    data = response.json()
    page_id = str(data.get("id", ""))
    page_name = data.get("name", "unknown")

    print(
        f"Page token identity check: {page_name} ({page_id})"
    )

    if page_id != str(FACEBOOK_PAGE_ID):
        fail(
            f"Resolved Page token belongs to {page_id}, "
            f"expected {FACEBOOK_PAGE_ID}."
        )


def _font(size: int, bold: bool = False):
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
    ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
) -> list[str]:
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


def create_branded_image(
    title: str,
    source: str,
    published: str,
) -> bytes:
    width, height = 1200, 675

    image = Image.new(
        "RGB",
        (width, height),
        (7, 20, 15),
    )
    draw = ImageDraw.Draw(image)

    # Pitch-style background
    draw.rectangle(
        (0, 350, width, height),
        fill=(10, 67, 39),
    )

    for x in range(-180, width + 240, 120):
        draw.line(
            (x, height, x + 260, 350),
            fill=(19, 95, 56),
            width=3,
        )

    for y in range(390, height, 54):
        draw.line(
            (0, y, width, y),
            fill=(19, 95, 56),
            width=2,
        )

    # Abstract ball
    center_x, center_y, radius = 1030, 125, 130

    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        outline=(235, 245, 240),
        width=4,
    )

    import math

    for angle in range(0, 360, 72):
        px = center_x + int(
            radius * 0.72 * math.cos(math.radians(angle))
        )
        py = center_y + int(
            radius * 0.72 * math.sin(math.radians(angle))
        )
        draw.line(
            (center_x, center_y, px, py),
            fill=(235, 245, 240),
            width=3,
        )

    white = (245, 250, 247)
    green = (64, 225, 156)
    muted = (185, 205, 195)

    brand_font = _font(33, True)
    label_font = _font(22, True)
    title_font = _font(53, True)
    small_font = _font(19, False)

    draw.text(
        (65, 42),
        "TWO TAKES EFOOTBALL",
        font=brand_font,
        fill=white,
    )

    draw.text(
        (65, 98),
        "NEWS",
        font=label_font,
        fill=green,
    )

    lines = wrap_text(
        draw,
        title,
        title_font,
        790,
    )

    y = 165

    for line in lines[:4]:
        draw.text(
            (65, y),
            line,
            font=title_font,
            fill=white,
        )
        y += 64

    source_text = source or "eFootball News"
    date_text = published[:16] if published else ""

    draw.text(
        (65, 555),
        f"{source_text}  •  {date_text}",
        font=small_font,
        fill=muted,
    )

    draw.text(
        (65, 605),
        "NEWS • UPDATES • RATINGS • EVENTS",
        font=label_font,
        fill=white,
    )

    out_path = WORK_PATH / "efootball_news_card.jpg"
    image.save(
        out_path,
        "JPEG",
        quality=92,
        optimize=True,
    )

    return out_path.read_bytes()


def publish_photo(
    caption: str,
    image_bytes: bytes,
    page_access_token: str,
) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/photos",
        data={
            "message": caption,
            "published": "true",
            "access_token": page_access_token,
        },
        files={
            "source": (
                "efootball-news.jpg",
                image_bytes,
                "image/jpeg",
            )
        },
        timeout=(15, 60),
    )

    if not response.ok:
        fail(
            f"Facebook API error {response.status_code}: "
            f"{response.text[:900]}"
        )

    result = response.json()

    if "id" not in result and "post_id" not in result:
        fail(
            "Facebook returned an unexpected publish response: "
            f"{result}"
        )

    return result


def cleanup_legacy_posts(page_access_token: str) -> None:
    legacy_ids = [
        "1397515220100650_122095343775487467",
        "1397515220100650_122095346487487467",
    ]

    for post_id in legacy_ids:
        try:
            response = requests.delete(
                f"https://graph.facebook.com/{post_id}",
                params={
                    "access_token": page_access_token,
                },
                timeout=(10, 30),
            )

            if response.ok:
                print(f"Removed legacy Facebook post {post_id}.")
            else:
                print(
                    f"Could not remove legacy Facebook post {post_id}: "
                    f"HTTP {response.status_code}"
                )

        except requests.RequestException as exc:
            print(
                f"Legacy post cleanup failed for {post_id}: {exc}"
            )


def main() -> None:
    if not FACEBOOK_PAGE_ID:
        fail("Missing FACEBOOK_PAGE_ID.")

    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        fail("Missing FACEBOOK_PAGE_ACCESS_TOKEN.")

    if SLOT not in SLOT_CONFIG:
        fail("POST_SLOT must be 1, 2, 3, or 4.")

    config = SLOT_CONFIG[SLOT]
    state = load_state()

    # Search multiple feeds so the bot is not dependent on one RSS query.
    collected = []
    seen_links = set()

    for query in config["queries"]:
        try:
            items = fetch_google_news(query)

            for item in items:
                if item["link"] in seen_links:
                    continue

                seen_links.add(item["link"])
                collected.append(item)

        except (
            requests.RequestException,
            ET.ParseError,
        ) as exc:
            print(f"News query failed: {query} | {exc}")

    collected.sort(
        key=lambda x: x["published_dt"],
        reverse=True,
    )

    story = choose_story(collected, state)
    caption_data = call_ai(story, config["name"])

    if "efootball" not in caption_data["caption"].lower():
        fail(
            "Generated post is not explicitly eFootball-specific."
        )

    if caption_data["caption"].strip().lower() == "none":
        fail("Generated post evaluated to None.")

    page_access_token = resolve_page_access_token(
        FACEBOOK_PAGE_ACCESS_TOKEN
    )
    verify_page_access(page_access_token)

    image_bytes = create_branded_image(
        caption_data["title"],
        story["source"],
        story["pub_date"],
    )

    result = publish_photo(
        caption_data["caption"],
        image_bytes,
        page_access_token,
    )

    cleanup_legacy_posts(page_access_token)

    entry = {
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "slot": SLOT,
        "type": "photo",
        "title": caption_data["title"],
        "tags": caption_data["tags"],
        "caption": caption_data["caption"],
        "story_title": story["title"],
        "source": story["source"],
        "pub_date": story["pub_date"],
        "link": story["link"],
        "facebook_id": result.get("post_id") or result.get("id"),
        "image_url": "generated://two-takes-efootball-news-card",
    }

    state.setdefault("posted", []).append(entry)
    state["posted"] = state["posted"][-100:]
    save_state(state)

    print(
        json.dumps(
            entry,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
