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

from discord_notify import notify_error, notify_facebook_post

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
    "OPENROUTER_MODEL",
    "openrouter/free",
).strip()

# The previous free GPT-OSS slug was retired. Keep older repository variables
# working by transparently routing them through OpenRouter's current free router.
if OPENROUTER_MODEL == "openai/gpt-oss-20b:free":
    OPENROUTER_MODEL = "openrouter/free"

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
            '"eFootball" KONAMI news when:3d',
        ],
        "required": ("efootball",),
        "exclude": ("ratings", "live update", "potw", "player of the week", "tactics", "formation", "guide", "tips"),
    },
    2: {
        "name": "Updates & Events",
        "queries": [
            'site:konami.com/efootball/en/topic/news eFootball update event campaign when:3d',
            '"eFootball" update event campaign maintenance when:3d',
        ],
        "required": ("update", "event", "campaign", "maintenance", "version", "season", "announcement"),
        "exclude": ("tactics", "formation", "guide", "tips"),
    },
    3: {
        "name": "Player Ratings",
        "queries": [
            'site:konami.com/efootball/en/topic/news eFootball ratings Live Update players when:3d',
            '"eFootball" POTW Epic Big Time Show Time ratings player when:3d',
        ],
        "required": ("rating", "live update", "potw", "player of the week", "epic", "big time", "show time", "booster", "player card"),
        "exclude": ("tactics", "formation", "guide"),
    },
    4: {
        "name": "Tips & Community",
        "queries": [
            '"eFootball" tactics formation guide tips Dream Team when:3d',
            '"eFootball" gameplay skills build community when:3d',
        ],
        "required": ("tactic", "formation", "guide", "tips", "gameplay", "skills", "build", "dream team"),
        "exclude": ("transfer", "real madrid", "premier league"),
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

        if title.upper().startswith("INFO DETAIL"):
            description_lower = description.lower()
            known_titles = [
                "Live Update Ratings Issue",
                "Regarding Unavailable Players and Managers",
                "Issue Affecting Some Player Models",
                "Update Notice",
            ]
            for known_title in known_titles:
                if known_title.lower() in description_lower:
                    title = known_title
                    break

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


def story_text(story: dict) -> str:
    return " ".join(
        [story.get("title", ""), story.get("description", "")]
    ).lower()


def is_efootball_story(story: dict, config: dict) -> bool:
    text = story_text(story)
    identity = (
        "efootball" in text
        or "e-football" in text
        or "e football" in text
        or "dream team" in text
        or "live update" in text
        or "potw" in text
        or "epic player" in text
        or "big time" in text
        or "show time" in text
    )
    if not identity:
        return False
    required = config.get("required", ())
    excluded = config.get("exclude", ())
    if required and not any(term in text for term in required):
        return False
    if any(term in text for term in excluded):
        return False
    return True


def choose_story(items: list[dict], state: dict, config: dict) -> dict:
    posted_links = {entry.get("link") for entry in state.get("posted", []) if entry.get("link")}
    posted_titles = {
        re.sub(r"\s+", " ", entry.get("title", "").strip().lower())
        for entry in state.get("posted", [])
        if entry.get("title")
    }
    candidates = []
    for item in items:
        normalized = re.sub(r"\s+", " ", item.get("title", "").strip().lower())
        if item.get("link") in posted_links or normalized in posted_titles:
            continue
        if not is_efootball_story(item, config):
            continue
        title = item.get("title", "").strip().lower()
        if title in {"info detail", "konami group corporation", "info detail - konami group corporation"} or len(title) <= 12:
            continue
        if not item.get("source", "").strip():
            continue
        source_lower = item.get("source", "").lower()
        score = 0
        if "konami" in source_lower:
            score += 100
        elif any(name in source_lower for name in ("gamingonphone", "efootballhub", "sportsdunia", "sportskeeda", "game8", "dexerto")):
            score += 30
        age_hours = max(0, (datetime.now(timezone.utc) - item["published_dt"]).total_seconds() / 3600)
        score -= min(int(age_hours), 72)
        candidates.append((score, item))
    if not candidates:
        fail(
            f"No fresh story matched the '{config['name']}' slot in the last 3 days. "
            "The bot will skip instead of publishing unrelated content."
        )
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    chosen = candidates[0][1]
    print(f"Selected story: {chosen['title']} | {chosen['source']} | {chosen['pub_date']}")
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


def _shorten_title(title: str, limit: int = 92) -> str:
    title = re.sub(
        r"\s+",
        " ",
        title.replace("eFootball™", "eFootball"),
    ).strip()

    if len(title) <= limit:
        return title

    # Prefer cutting at a natural punctuation boundary.
    for marker in (":", " - ", " — ", " | "):
        pos = title.find(marker)
        if 30 <= pos <= limit:
            return title[:pos].rstrip(" :-—|")

    words = title[:limit + 1].rsplit(" ", 1)
    shortened = words[0].rstrip(" ,:-—")
    return shortened or title[:limit].rstrip()


def _topic_fallback_title(source_title: str) -> str:
    title = source_title.replace("eFootball™", "eFootball").strip()
    lower = title.lower()

    if "international match campaign" in lower:
        return "eFootball 2027: International Match Campaign Brings Free Chance Deals"

    if "national all-stars" in lower:
        return "eFootball 2027: National All-Stars Content Arrives"

    if "live update" in lower or "ratings issue" in lower:
        return "eFootball Live Update: KONAMI Reports a Player Ratings Issue"

    if "unavailable players" in lower and "managers" in lower:
        return "eFootball: KONAMI Addresses Unavailable Players & Managers"

    if "epic" in lower and "big time" in lower:
        return "eFootball 2027: New Epic & Big Time Cards Revealed"

    if "show time" in lower:
        return "eFootball 2027: New Show Time Player Cards Revealed"

    if "potw" in lower or "player of the week" in lower:
        return "eFootball 2027: New POTW Player Cards Arrive"

    return _shorten_title(title, 88)


def _topic_fallback_body(story: dict) -> str:
    title = story["title"].split(" - ")[0].strip()
    clean = story.get("description", "").strip()
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"(?i)\s*&nbsp;\s*", " ", clean)
    clean = clean.strip(" -")

    lower = title.lower()

    if "international match campaign" in lower:
        body = (
            "The International Match Campaign is now the focus in eFootball 2027, "
            "with free Chance Deals and National All-Stars content featured in the rollout. "
            "Eric Cantona is also part of the campaign lineup. 🔥🎁"
        )
    elif "live update" in lower or "ratings issue" in lower:
        body = (
            "KONAMI has reported an issue affecting eFootball Live Update player ratings "
            "and says the problem is being addressed. Keep an eye on the next maintenance "
            "for the latest status. 🚨📊"
        )
    elif "unavailable players" in lower and "managers" in lower:
        body = (
            "KONAMI has published an eFootball notice about unavailable players and managers. "
            "Check the official update details before making changes to your squad. 👀"
        )
    elif clean and clean.lower() != title.lower():
        sentences = re.split(r"(?<=[.!?])\s+", clean)
        usable = " ".join(sentences[:2]).strip()
        body = usable[:650]
    else:
        body = (
            f"{_topic_fallback_title(title)}. "
            "We’re tracking the latest eFootball details, player content and updates. 🔥"
        )

    return body


def deterministic_post(story: dict) -> dict:
    title = _topic_fallback_title(story["title"].split(" - ")[0].strip())
    body = _topic_fallback_body(story)

    if "efootball" not in title.lower():
        title = "eFootball: " + title

    version_tag = (
        "#eFootball2027"
        if re.search(r"\b2027\b", title, flags=re.I)
        else "#eFootball"
    )

    tags = []
    for tag in (
        "#eFootball",
        version_tag,
        "#KONAMI",
        "#eFootballNews",
        "#DreamTeam",
    ):
        if tag.lower() not in {x.lower() for x in tags}:
            tags.append(tag)

    tags_text = " ".join(tags)

    return {
        "title": title,
        "body": body[:700],
        "tags": tags_text,
        "caption": (
            f"{title}\n\n"
            f"{body[:700]}\n\n"
            f"Source: {story.get('source', 'eFootball News')}\n\n"
            f"{tags_text}"
        ),
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

    if not title or title.lower() in {
        "info detail",
        "konami group corporation",
        "info detail - konami group corporation",
    }:
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

Write exactly:
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
        "max_tokens": 420,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/iammehrub/two-takes-efootball",
        "X-Title": "Two Takes EFootball",
    }

    models = [OPENROUTER_MODEL]
    last_error = ""

    for model in models:
        payload["model"] = model

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
                last_error = f"{model}: {type(exc).__name__}: {exc}"

                if attempt < 3:
                    delay = min(12, 2 ** attempt) + random.random()
                    print(
                        f"OpenRouter {model} network error; "
                        f"retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    continue

                break

            if response.ok:
                try:
                    data = response.json()
                except ValueError:
                    last_error = f"{model}: invalid JSON response."
                    break

                choices = data.get("choices") or []

                if choices:
                    message = choices[0].get("message") or {}
                    content = _clean_ai_text(message.get("content"))

                    if not content:
                        content = _clean_ai_text(
                            choices[0].get("text")
                        )

                    if content:
                        parsed = parse_generated_post(
                            content,
                            story,
                        )
                        print(
                            f"Caption generated with OpenRouter model {model}."
                        )
                        return parsed

                    finish_reason = choices[0].get("finish_reason")
                    last_error = (
                        f"{model}: no final text "
                        f"(finish_reason={finish_reason})."
                    )
                else:
                    last_error = f"{model}: no choices returned."

                # A successful HTTP response with no usable text is not
                # transient enough to keep hammering the same model.
                break

            last_error = (
                f"{model}: HTTP {response.status_code}: "
                f"{response.text[:700]}"
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    delay = min(12, 2 ** attempt) + random.random()
                    print(
                        f"OpenRouter {model} returned "
                        f"{response.status_code}; retrying in "
                        f"{delay:.1f}s..."
                    )
                    time.sleep(delay)
                    continue

            break

        print(
            f"Model {model} did not produce a usable caption; "
            "trying the next option."
        )

    print(
        "OpenRouter unavailable for final text; using deterministic "
        f"source-based caption. Last error: {last_error}"
    )
    return deterministic_post(story)



def resolve_page_access_token(token: str) -> str:
    """Accept either a Page token directly or a User token that can resolve to the Page."""
    global FACEBOOK_PAGE_ID

    if not token:
        fail("Missing FACEBOOK_PAGE_ACCESS_TOKEN.")

    # Preferred path: the repository secret is already a Page Access Token.
    # This avoids a dependency on /me/accounts and lets long-lived Page tokens
    # be used directly.
    page_check = requests.get(
        f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}",
        params={
            "fields": "id,name",
            "access_token": token,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=(10, 30),
    )
    if page_check.ok:
        data = page_check.json()
        page_id = str(data.get("id", ""))
        if page_id == str(FACEBOOK_PAGE_ID):
            print(
                f"Using supplied Page Access Token for "
                f"{data.get('name', 'unknown')} ({page_id})."
            )
            return token

    # Backward-compatible path: accept a User Access Token and derive the
    # configured Page token from /me/accounts.
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
            "Facebook token is neither a usable Page token nor a User token "
            f"that can list Pages. HTTP {response.status_code}: "
            f"{response.text[:700]}"
        )

    pages = response.json().get("data", [])

    if not pages:
        fail(
            "This Facebook token has access to no Pages. "
            "Use a long-lived token from the Facebook account that manages "
            "the Page, then derive the Page Access Token from /me/accounts."
        )

    for page in pages:
        page_id = str(page.get("id", ""))
        if page_id != str(FACEBOOK_PAGE_ID):
            continue

        page_token = str(page.get("access_token", "")).strip()
        if not page_token:
            fail(
                f"Meta found the Page {page.get('name', 'unknown')} "
                f"({page_id}) but returned no Page Access Token."
            )

        tasks = page.get("tasks") or []
        print(
            f"Resolved Page: {page.get('name', 'unknown')} ({page_id})."
        )
        print(
            "Page tasks: "
            + (", ".join(tasks) if tasks else "not returned")
        )
        return page_token

    visible = [
        f"{page.get('name', 'unknown')} ({page.get('id', 'unknown')})"
        for page in pages
    ]

    if len(pages) == 1:
        page = pages[0]
        page_id = str(page.get("id", ""))
        page_token = str(page.get("access_token", "")).strip()
        if page_token:
            print(
                f"Configured Page ID did not match. Using the only Page visible "
                f"to this token: {page.get('name', 'unknown')} ({page_id})."
            )
            FACEBOOK_PAGE_ID = page_id
            return page_token

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



def validate_generated_post(post: dict, story: dict, config: dict) -> None:
    title = re.sub(r"\s+", " ", post.get("title", "").strip())
    body = re.sub(r"\s+", " ", post.get("body", "").strip())
    caption = post.get("caption", "").strip()
    if not title or not body or not caption:
        fail("Generated post is empty or incomplete.")
    title_lower = title.lower()
    if not ("efootball" in title_lower or any(term in title_lower for term in config.get("required", ()))):
        fail("Generated title is not specific enough for the selected eFootball slot.")
    if len(title) > 110:
        fail("Generated title is too long.")
    if len(body) < 25:
        fail("Generated body is too short to be useful.")
    if re.fullmatch(r"(?i)(none|null|n/a)", title):
        fail("Generated title is invalid.")
    source_text = story_text(story)
    required = config.get("required", ())
    if required and not any(term in source_text for term in required):
        fail("Source does not contain the required topic for this slot; refusing to publish.")
    if "efootball" not in caption.lower():
        fail("Generated post is not explicitly eFootball-specific.")

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

    story = choose_story(collected, state, config)
    caption_data = call_ai(story, config["name"])
    validate_generated_post(caption_data, story, config)

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

    # Discord notification is deliberately non-blocking: a Discord outage
    # must not turn a successful Facebook publication into a failed post run.
    notify_facebook_post(entry)

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
        notify_error(str(exc), component="Facebook posting")
        raise
