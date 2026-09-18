# Two Takes EFootball

Automated Facebook Page posting for Two Takes EFootball.

## What it does

- Publishes 4 posts per day.
- Uses Google News RSS to find fresh eFootball-related stories.
- Uses Gemini to turn the source into an English/Banglish Facebook caption.
- Uses Pexels for a landscape football image when available.
- Publishes the image + caption to the Facebook Page through the Graph API.
- Keeps a small posting history to reduce duplicate stories.

## GitHub Secrets

Create these repository secrets under Settings → Secrets and variables → Actions:

- FACEBOOK_PAGE_ACCESS_TOKEN
- FACEBOOK_PAGE_ID
- GEMINI_API_KEY
- PEXELS_API_KEY

## Schedule

The workflow uses Bangladesh time (Asia/Dhaka) and targets 10:15, 14:15, 18:15, and 22:15.

GitHub scheduled workflows can be delayed under load, so the exact posting minute is not guaranteed.

## Manual test

Open Actions → Two Takes EFootball → Run workflow and select a post slot.
