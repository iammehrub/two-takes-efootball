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
    full_fields = "reactions.limit(0).summary(true),comments.limit(0).summary(true),shares"
    response = requests.get(
        f"https://graph.facebook.com/{post_id}",
        params={"fields": full_fields, "access_token": token},
        timeout=(10, 30),
    )

    if response.ok:
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
            "metrics_limited": False,
        }

    # Meta may block reaction/comment reads unless the app has
    # pages_read_user_content or Page Public Content Access. Keep analytics
    # useful and stop error spam by falling back to shares, which this
    # Page-token flow can still expose.
    try:
        error_body = response.json().get("error", {})
    except ValueError:
        error_body = {}

    if response.status_code == 400 and str(error_body.get("code")) == "10":
        shares_response = requests.get(
            f"https://graph.facebook.com/{post_id}",
            params={"fields": "shares", "access_token": token},
            timeout=(10, 30),
        )
        if shares_response.ok:
            data = shares_response.json()
            shares_node = data.get("shares") or {}
            try:
                shares = int(shares_node.get("count", 0) or 0)
            except (TypeError, ValueError):
                shares = 0
            print(
                f"Facebook reaction/comment metrics unavailable for {post_id}; "
                "falling back to shares-only analytics."
            )
            return {
                "reactions": None,
                "comments": None,
                "shares": shares,
                "visible_interactions": None,
                "metrics_limited": True,
            }

    raise RuntimeError(
        f"Facebook metrics HTTP {response.status_code}: {response.text[:700]}"
    )


def resolve_page_access_token(token: str, page_id: str) -> str:
    """Accept a Page token directly; otherwise derive one from a User token."""
    if not token:
        raise RuntimeError("Missing FACEBOOK_PAGE_ACCESS_TOKEN.")

    # Preferred path: the configured secret is already a Page token.
    page_check = requests.get(
        f"https://graph.facebook.com/{page_id}",
        params={"fields": "id,name", "access_token": token},
        timeout=(10, 30),
    )
    if page_check.ok:
        data = page_check.json()
        if str(data.get("id", "")) == str(page_id):
            print(
                f"Using supplied Page Access Token for "
                f"{data.get('name', 'unknown')} ({page_id})."
            )
            return token

    # Backward-compatible path: a User token can be used to resolve the
    # Page token through /me/accounts.
    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={
            "fields": "id,name,access_token,tasks",
            "access_token": token,
        },
        timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(
            "Facebook token is neither a usable Page token nor a User token "
            f"that can list Pages. HTTP {response.status_code}: "
            f"{response.text[:700]}"
        )

    pages = response.json().get("data", [])
    for page in pages:
        if str(page.get("id", "")) != str(page_id):
            continue
        page_token = str(page.get("access_token", "")).strip()
        if page_token:
            return page_token

    if len(pages) == 1:
        page_token = str(pages[0].get("access_token", "")).strip()
        if page_token:
            return page_token

    raise RuntimeError(
        f"Facebook Page {page_id} was not returned by this User token."
    )


def fetch_recent_page_posts(page_id: str, token: str) -> list[dict]:
    response = requests.get(
        f"https://graph.facebook.com/{page_id}/posts",
        params={
            "fields": "id,created_time,message",
            "limit": 25,
            "access_token": token,
        },
        timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(
            f"Facebook recent-post lookup failed: HTTP {response.status_code}: {response.text[:700]}"
        )
    result = []
    for item in response.json().get("data", []):
        post_id = str(item.get("id", "")).strip()
        created = parse_dt(item.get("created_time", ""))
        message = str(item.get("message", "")).strip()
        if post_id and created:
            title = message.splitlines()[0].strip() if message else "Facebook post"
            result.append({
                "facebook_id": post_id,
                "posted_at": created.isoformat(),
                "title": title[:110],
                "slot": "?",
            })
    return result

def build_summary(a: dict) -> str:
    if a.get("metrics_limited"):
        text = (
            f"At about {a['age_hours']:.1f} hours, Facebook returned "
            f"{a['shares']} shares. Reaction and comment counts are unavailable "
            "for the current app permissions, so this is a limited engagement snapshot."
        )
    else:
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

    page_token = resolve_page_access_token(token, page_id)

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

    # Backfill a recent Facebook Page post when repository state is empty.
    # This lets the analytics workflow test existing Page content and recover
    # from state resets without requiring a new publication first.
    if not eligible:
        try:
            recent_posts = fetch_recent_page_posts(page_id, page_token)
            for entry in recent_posts:
                post_id = str(entry.get("facebook_id", "")).strip()
                posted_at = parse_dt(entry.get("posted_at", ""))
                if not post_id or not posted_at or post_id in processed:
                    continue
                age_hours = (now - posted_at).total_seconds() / 3600
                if age_hours >= 12:
                    eligible.append((posted_at, entry, age_hours))
            eligible.sort(key=lambda x: x[0])
        except Exception as exc:
            print(f"Facebook backfill lookup failed: {exc}")

    if not eligible:
        print("No Facebook posts are ready for 12-hour analysis.")
        return

    for _, entry, age_hours in eligible:
        post_id = str(entry["facebook_id"])
        try:
            metrics = fetch_metrics(post_id, page_token)
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
