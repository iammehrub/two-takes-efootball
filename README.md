# Two Takes EFootball

Automated Facebook Page publishing and analytics for Two Takes EFootball.

## Automation

- Publishes 4 scheduled posts per day at 10:15, 14:15, 18:15, and 22:15 Bangladesh time.
- Slot 1 = eFootball news.
- Slot 2 = updates and events.
- Slot 3 = player ratings / player-card news.
- Slot 4 = tips and community content.
- Uses Google News RSS to find fresh stories and prefers official KONAMI material.
- Uses OpenRouter for captions with source-grounded validation and a deterministic fallback.
- Generates a branded 1200×675 image locally with Pillow.
- Records published posts in `data/state.json` to reduce duplicates.
- Runs Facebook 12-hour analytics every hour and only processes posts that are at least 12 hours old.
- Sends Discord alerts for published posts, analytics, and workflow failures.

## Required GitHub Secrets

Create these under **Settings → Secrets and variables → Actions**:

- `FACEBOOK_PAGE_ACCESS_TOKEN`
- `FACEBOOK_PAGE_ID`
- `OPENROUTER_API_KEY`
- `DISCORD_FACEBOOK_WEBHOOK`
- `DISCORD_FACEBOOK_ANALYTICS_WEBHOOK`
- `DISCORD_ALERTS_WEBHOOK`

Optional repository variable:

- `OPENROUTER_MODEL` — defaults to `openrouter/free`.

## Manual testing

Open **Actions → Two Takes EFootball — Post → Run workflow** and choose slot 1–4.

Open **Actions → Two Takes EFootball — 12h Analytics → Run workflow** to test analytics.

## Failure handling

Facebook posting and analytics state are committed back to the repository with retry/rebase logic. A separate failure-alert workflow sends a Discord alert when either automated workflow fails.

## Repository split

This repository handles Facebook/eFootball automation and Facebook analytics.

The YouTube podcast automation and Bangladesh news feed stay in `iammehrub/the-two-takes`.
