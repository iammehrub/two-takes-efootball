# Two Takes EFootball

Automated Facebook Page posting for Two Takes EFootball.

## What it does

- Publishes up to 4 posts per day.
- Uses Google News RSS to find fresh eFootball stories.
- Uses slot-specific topic filters so news, updates/events, player-card/rating content, and tips/community content do not get mixed together.
- Prefers official KONAMI material and established eFootball coverage.
- Uses OpenRouter for English/Banglish captions with a deterministic source-based fallback.
- Generates a branded 1200×675 image locally with Pillow.
- Publishes the image + caption to the Facebook Page through the Graph API.
- Keeps posting history in `data/state.json` to reduce duplicate stories.
- Refuses to publish when no suitable fresh story is found.

## GitHub Secrets

Create these repository secrets under Settings → Secrets and variables → Actions:

- FACEBOOK_PAGE_ACCESS_TOKEN
- FACEBOOK_PAGE_ID
- OPENROUTER_API_KEY
- DISCORD_FACEBOOK_WEBHOOK
- DISCORD_FACEBOOK_ANALYTICS_WEBHOOK
- DISCORD_ALERTS_WEBHOOK

Optional repository variable:

- `OPENROUTER_MODEL` — defaults to `openai/gpt-oss-20b:free`

## Schedule

The workflow uses Bangladesh time (Asia/Dhaka) and targets 10:15, 14:15, 18:15, and 22:15.

GitHub scheduled workflows can be delayed under load, so the exact posting minute is not guaranteed.

## Manual test

Open Actions → Two Takes EFootball → Run workflow and select a post slot.

## Discord split

This repository is responsible for Facebook content and Facebook analytics.

The YouTube upload notifications and Bangladesh news feed are intentionally kept in the separate `iammehrub/the-two-takes` repository. This prevents the Facebook workflow from requiring YouTube channel IDs and caused the previous YouTube-variable alert spam to stop.
