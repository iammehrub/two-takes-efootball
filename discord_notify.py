import os
import requests

USER_AGENT = "TwoTakesEFootballBot/3.0 (+GitHub Actions)"


def _truncate(value: str, limit: int) -> str:
    value = str(value or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def send_webhook(env_name: str, title: str, description: str, *, fields=None, url: str = "", footer: str = "Two Takes Automation") -> bool:
    webhook = os.environ.get(env_name, "").strip()
    if not webhook:
        print(f"Discord webhook {env_name} is not configured; skipping notification.")
        return False
    embed_fields = []
    for name, value, inline in (fields or []):
        embed_fields.append({"name": _truncate(name, 256), "value": _truncate(value, 1024), "inline": bool(inline)})
    embed = {"title": _truncate(title, 256), "description": _truncate(description, 4096), "fields": embed_fields, "footer": {"text": _truncate(footer, 2048)}}
    if url:
        embed["url"] = url
    elif _run_url():
        embed["url"] = _run_url()
    payload = {"username": "Two Takes Automation", "embeds": [embed], "allowed_mentions": {"parse": []}}
    try:
        response = requests.post(webhook, json=payload, headers={"User-Agent": USER_AGENT}, timeout=(10, 20))
        if response.status_code not in (200, 204):
            print(f"Discord webhook failed: HTTP {response.status_code}: {response.text[:700]}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"Discord webhook request failed: {exc}")
        return False


def notify_facebook_post(entry: dict) -> bool:
    post_id = str(entry.get("facebook_id", "")).strip()
    return send_webhook(
        "DISCORD_FACEBOOK_WEBHOOK",
        "🟢 Facebook Post Published",
        "A new Two Takes EFootball Facebook post was published.",
        fields=[
            ("Slot", str(entry.get("slot", "?")), True),
            ("Title", entry.get("title", "Untitled"), False),
            ("Source", entry.get("source", "Unknown"), True),
            ("Post ID", post_id or "Not returned", True),
        ],
        url=f"https://www.facebook.com/{post_id}" if post_id else "",
        footer="Facebook • Two Takes EFootball",
    )


def notify_facebook_analytics(analysis: dict) -> bool:
    baseline = analysis.get("baseline")
    comparison = "No same-slot baseline yet."
    delta = analysis.get("delta_percent")
    if isinstance(baseline, (int, float)) and baseline > 0 and isinstance(delta, (int, float)):
        sign = "+" if delta >= 0 else ""
        comparison = f"{sign}{delta:.1f}% vs recent same-slot average"
    return send_webhook(
        "DISCORD_FACEBOOK_ANALYTICS_WEBHOOK",
        "📊 12-Hour Facebook Analysis",
        analysis.get("summary", "Analysis completed."),
        fields=[
            ("Post", analysis.get("title", "Untitled"), False),
            ("Slot", str(analysis.get("slot", "?")), True),
            ("Reactions", str(analysis.get("reactions", 0)), True),
            ("Comments", str(analysis.get("comments", 0)), True),
            ("Shares", str(analysis.get("shares", 0)), True),
            ("Visible interactions", str(analysis.get("visible_interactions", 0)), True),
            ("Comparison", comparison, False),
        ],
        url=analysis.get("facebook_url", ""),
        footer="Facebook • 12-Hour Analytics",
    )


def notify_error(message: str, *, component: str = "Automation") -> bool:
    return send_webhook(
        "DISCORD_ALERTS_WEBHOOK",
        "🚨 Automation Error",
        _truncate(message, 3500),
        fields=[("Component", component, True)],
        footer="System Alerts • Facebook Repo",
    )
