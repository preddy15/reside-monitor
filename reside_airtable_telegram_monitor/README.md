# Reside Airtable → Telegram monitor

This project checks the public Airtable form every 5 minutes and watches the
`Project Applying For` dropdown.

On the first successful run it saves the existing options as the baseline.
After that, any newly appearing option triggers a Telegram message.

## 1. Create the Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`.
3. Choose a name and username.
4. BotFather will give you a bot token. Keep it private.
5. Open your new bot in Telegram and send it `/start`.

## 2. Get your Telegram chat ID

After sending `/start`, run this on your computer, replacing `YOUR_TOKEN`:

```bash
curl "https://api.telegram.org/botYOUR_TOKEN/getUpdates"
```

Find:

```json
"chat": {
  "id": 123456789
}
```

That number is your `TELEGRAM_CHAT_ID`.

If `result` is empty, send another message to your bot and run the command again.

## 3. Create the GitHub repository

Create a new GitHub repository and upload the contents of this folder.

If you want GitHub-hosted Actions to remain free at high frequency, use a
**public repository**. The Telegram token and chat ID will still be stored as
GitHub Secrets, not in the repository files.

## 4. Add the two GitHub Secrets

Repository → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**

Add:

- `TELEGRAM_BOT_TOKEN` = token from BotFather
- `TELEGRAM_CHAT_ID` = your numeric chat ID

Never put the token directly into `monitor.py` or the workflow file.

## 5. Enable and test

1. Open the repository's **Actions** tab.
2. Select **Monitor Airtable dropdown**.
3. Click **Run workflow**.
4. The first successful run should send:

   `✅ Reside Airtable monitor initialized.`

5. From then on it checks roughly every 5 minutes and alerts only when a new
   dropdown item appears.

## What it monitors

- Form:
  https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form
- Field:
  `Project Applying For`

## If Airtable changes the page

The workflow saves a screenshot and HTML file as a GitHub Actions artifact when
scraping fails. Open the failed workflow run and download `airtable-debug`.

## Notes

- Scheduled GitHub Actions are not guaranteed to run at the exact scheduled
  second; there can be delays.
- The current snapshot is stored in `state.json`.
- Existing items do not trigger alerts on the first run.
- If an item disappears and later reappears, it will alert again.
