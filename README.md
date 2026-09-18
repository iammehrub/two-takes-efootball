# Two Takes EFootball

Automated Facebook Page posting for Two Takes EFootball.

## What it does

- Publishes 4 posts per day.
- Uses Google News RSS to find fresh eFootball-related stories.
- Uses OpenRouter's free-model router to turn the source into an English/Banglish Facebook caption.
- Generates a branded 1200×675 image locally with Pillow.
- Publishes the image + caption to the Facebook Page through the Graph API.
- Keeps posting history in `data/state.json` to reduce duplicate stories.
- Refuses to publish when no suitable fresh story is found.

## GitHub Secrets

Create these repository secrets under Settings → Secrets and variables → Actions:

- FACEBOOK_PAGE_ACCESS_TOKEN
- FACEBOOK_PAGE_ID
- OPENROUTER_API_KEY

## Schedule

The workflow uses Bangladesh time (Asia/Dhaka) and targets 10:15, 14:15, 18:15, and 22:15.

GitHub scheduled workflows can be delayed under load, so the exact posting minute is not guaranteed.

## Manual test

Open Actions → Two Takes EFootball → Run workflow and select a post slot.

## Production safety

- There is no push trigger, so editing the repository does not automatically publish a Facebook post.
- Legacy Facebook post deletion is not part of the normal posting workflow.
- The bot validates the selected story and generated caption before publishing.
