import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

from discord_notify import notify_error, send_webhook

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "youtube_state.json"


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_feed(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = requests.get(url, headers={"User-Agent": "TwoTakesYouTubeWatcher/1.0"}, timeout=(10, 20))
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"yt": "http://www.youtube.com/xml/schemas/2015", "atom": "http://www.w3.org/2005/Atom"}
    items = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
        link_node = entry.find("atom:link", ns)
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        author = entry.find("atom:author/atom:name", ns)
        channel = author.text.strip() if author is not None and author.text else ""
        if not video_id or not title or not link:
            continue
        items.append({
            "video_id": video_id,
            "title": title,
            "published": published or updated,
            "link": link,
            "channel": channel,
        })
    return items


def main() -> None:
    configs = [
        ("podcast", "YOUTUBE_PODCAST_CHANNEL_ID", "DISCORD_YOUTUBE_PODCAST_WEBHOOK", "Podcast"),
        ("shorts", "YOUTUBE_SHORTS_CHANNEL_ID", "DISCORD_YOUTUBE_SHORTS_WEBHOOK", "Shorts"),
    ]

    state = load_state()
    seen = set(str(x) for x in state.get("seen_video_ids", []))
    configured = False
    new_videos = []
    first_run = not state.get("initialized", False)

    for _, channel_env, webhook_env, label in configs:
        channel_id = os.environ.get(channel_env, "").strip()
        if not channel_id:
            print(f"{channel_env} is not configured; skipping {label}.")
            continue
        configured = True
        try:
            feed = fetch_feed(channel_id)
        except Exception as exc:
            notify_error(str(exc), component=f"YouTube {label} watcher")
            continue

        # On the first run, seed the current feed without sending a
        # notification for older uploads already present in the channel feed.
        if first_run:
            seen.update(video["video_id"] for video in feed)
            print(f"Initialized {label} with {len(feed)} existing videos.")
            continue

        for video in reversed(feed):
            video_id = video["video_id"]
            if video_id in seen:
                continue

            ok = send_webhook(
                webhook_env,
                f"🎬 YouTube {label} Published",
                f"A new {label.lower()} video was detected.",
                fields=[
                    ("Title", video["title"], False),
                    ("Published", video["published"], True),
                    ("Channel", video["channel"] or label, True),
                ],
                url=video["link"],
                footer=f"YouTube • {label}",
            )
            if ok:
                seen.add(video_id)
                new_videos.append(video_id)
                print(f"Notified {label}: {video['title']}")
            else:
                print(f"Discord notification failed for {label}: {video['title']}")

    if not configured:
        raise RuntimeError(
            "Configure YOUTUBE_PODCAST_CHANNEL_ID and/or YOUTUBE_SHORTS_CHANNEL_ID in GitHub Variables."
        )

    state["seen_video_ids"] = list(seen)[-200:]
    state["initialized"] = True
    state["checked_at"] = datetime.now(timezone.utc).isoformat()
    state["new_videos"] = new_videos
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        notify_error(str(exc), component="YouTube watcher")
        raise
