import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

import requests

from discord_notify import notify_error, notify_facebook_analytics

ROOT = Path(__file__).resolve().parent
POST_STATE_PATH = ROOT / "data" / "state.json"
ANALYTICS_STATE_PATH = ROOT / "data" / "analytics_state.json"


def load_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_dt(value: str):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def count_summary(node) -> int:
    if not isinstance(node, dict):
        return 0
    summary = node.get("summary")
    if isinstance(summary, dict):
        try:
            return int(summary.get("total_count", 0) or 0)
        except (TypeError, ValueError):
            pass
    try:
        return int(node.get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def fetch_metrics(post_id: str, token: str) -> dict:
    fields = "reactions.limit(0).summary(true),comments.limit(0).summary(true),shares"
    response = requests.get(
        f"https://graph.facebook.com/{post_id}",
        params={"fields": fields, "access_token": token},
        timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(f"Facebook metrics HTTP {response.status_code}: {response.text[:700]}")
    data = response.json()
    reactions = count_summary(data.get("reactions"))
    comments = count_summary(data.get("comments"))
    shares_node = data.get("shares") or {}
    try:
        shares = int(shares_node.get("count", 0) or 0)
    except (TypeError, ValueError):
        shares = 0
    return {
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
        "visible_interactions": reactions + comments + shares,
    }


def build_summary(a: dict) -> str:
    text = (
        f"At about {a['age_hours']:.1f} hours, Facebook returned "
        f"{a['visible_interactions']} visible interactions: "
        f"{a['reactions']} reactions, {a['comments']} comments, and {a['shares']} shares."
    )
    if a.get("baseline") is not None and a.get("delta_percent") is not None:
        direction = "above" if a["delta_percent"] > 0 else "below" if a["delta_percent"] < 0 else "at"
        text += (
            f" That is {abs(a['delta_percent']):.1f}% {direction} "
            f"the recent same-slot average of {a['baseline']:.0f}."
        )
    text += " This is an engagement snapshot, not a reach/impressions measurement."
    return text


def main() -> None:
    token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    page_id = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    if not token or not page_id:
        raise RuntimeError("Missing FACEBOOK_PAGE_ACCESS_TOKEN or FACEBOOK_PAGE_ID.")

    posted_state = load_json(POST_STATE_PATH, {"posted": []})
    analytics_state = load_json(ANALYTICS_STATE_PATH, {"processed": [], "history": []})
    processed = {str(x) for x in analytics_state.get("processed", [])}
    history = [x for x in analytics_state.get("history", []) if isinstance(x, dict)]

    now = datetime.now(timezone.utc)
    eligible = []
    for entry in posted_state.get("posted", []):
        post_id = str(entry.get("facebook_id", "")).strip()
        posted_at = parse_dt(entry.get("posted_at", ""))
        if not post_id or not posted_at or post_id in processed:
            continue
        age_hours = (now - posted_at).total_seconds() / 3600
        if age_hours >= 12:
            eligible.append((posted_at, entry, age_hours))

    eligible.sort(key=lambda x: x[0])
    if not eligible:
        print("No Facebook posts are ready for 12-hour analysis.")
        return

    for _, entry, age_hours in eligible:
        post_id = str(entry["facebook_id"])
        try:
            metrics = fetch_metrics(post_id, token)
        except Exception as exc:
            print(f"Could not analyze {post_id}: {exc}")
            notify_error(str(exc), component="Facebook 12-hour analytics")
            continue

        baseline_values = [
            int(row.get("visible_interactions", 0))
            for row in history[-50:]
            if row.get("slot") == entry.get("slot")
            and row.get("post_id") != post_id
            and isinstance(row.get("visible_interactions"), (int, float))
        ][-10:]
        baseline = statistics.mean(baseline_values) if baseline_values else None
        delta = None if not baseline else ((metrics["visible_interactions"] - baseline) / baseline) * 100

        analysis = {
            "analyzed_at": now.isoformat(),
            "age_hours": round(age_hours, 2),
            "post_id": post_id,
            "facebook_url": f"https://www.facebook.com/{post_id}",
            "title": entry.get("title", "Untitled"),
            "slot": entry.get("slot", "?"),
            **metrics,
            "baseline": baseline,
            "delta_percent": delta,
        }
        analysis["summary"] = build_summary(analysis)

        if notify_facebook_analytics(analysis):
            processed.add(post_id)
            history.append(analysis)
            print(f"Analysis sent for {post_id}.")
        else:
            print(f"Discord analytics notification failed for {post_id}; will retry later.")

    analytics_state["processed"] = list(processed)[-500:]
    analytics_state["history"] = history[-100:]
    save_json(ANALYTICS_STATE_PATH, analytics_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        notify_error(str(exc), component="Facebook 12-hour analytics")
        raise
