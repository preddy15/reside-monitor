# Reside Airtable → Telegram monitor (v2)

This version explicitly follows the form interaction:

1. Open the Reside Airtable form.
2. Find `Project Applying For`.
3. Click `Add unit`.
4. If Airtable shows another selector, open it.
5. Read all visible unit/project choices.
6. Compare them to the previous snapshot.
7. Telegram-alert only newly appearing choices.

The first successful run records the current list as the baseline.

## Telegram secrets

Add these under:

GitHub repository → Settings → Secrets and variables → Actions

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Run it

Actions → Monitor Airtable dropdown → Run workflow

On the first run, the bot should send an initialization message confirming
that the monitor clicked `Add unit`.

## Debugging

If Airtable changes its UI, a failed workflow uploads an `airtable-debug`
artifact containing a screenshot and page HTML.
