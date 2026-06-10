# Driving Test Slot Watcher — Skåne

Checks Trafikverket every hour for körprov B slots across 11 Skåne locations.
Sends a Telegram notification when a slot appears before the cutoff date
(default `2026-07-15`). The latest full snapshot is always in [REPORT.md](REPORT.md).

## How it works

- `check_slots.py` polls `fp.trafikverket.se/Boka/occasion-bundles` for each
  location in `locations.json` (3s between requests).
- Slots before `CUTOFF_DATE` trigger one Telegram message per *new* slot batch.
  Already-notified slots are tracked in `state.json` — no repeat pings.
- August-and-later slots never notify, but show up in `REPORT.md`.
- GitHub Actions runs it every 20 minutes (`.github/workflows/check.yml`)
  and commits the updated report back to the repo.

### Session keep-alive

Trafikverket sessions expire after **30 minutes of inactivity** (sliding
window) and the `FpsExternalIdentity` token rotates on every response.
The script honors `Set-Cookie` headers and persists rotated cookies to
`cookie_store.enc` (AES-256 encrypted with the `COOKIE_KEY` secret,
committed by CI). With runs every 20 minutes the session stays alive
indefinitely — until GitHub skips enough scheduled runs to exceed the
30-minute window. Then you get one Telegram alert and must log in again.
The `TRV_COOKIE` secret is only the *seed*: it is used when its
`LoginValid` timestamp is newer than the store's (i.e. right after you
refresh it following a fresh login).

## Setup

### 1. Telegram bot (~2 min)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name → copy the **token**.
2. Open your new bot's chat and send it any message (e.g. "hi").
3. Get your chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Look for `"chat": {"id": ...}`.

### 2. GitHub secrets

```bash
gh secret set TRV_COOKIE          # full Cookie header from a logged-in browser session
gh secret set TRV_SSN             # personnummer, e.g. YYYYMMDD-XXXX
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

### 3. Run

Manual trigger: `gh workflow run check.yml`, or wait for the hourly cron.

## Cookie refresh runbook

The Trafikverket session cookie expires. When it does, you get one Telegram
alert ("session cookie expired") and runs fail until refreshed:

1. Log in at <https://fp.trafikverket.se/Boka/> (BankID).
2. DevTools → Network tab → click any `occasion-bundles` request →
   Request Headers → copy the full `Cookie` value.
3. `gh secret set TRV_COOKIE` and paste it.
4. `gh workflow run check.yml` to confirm green.

## Local run

```bash
export TRV_COOKIE='...'
export TRV_SSN='...'
export TELEGRAM_BOT_TOKEN='...'   # optional; prints message instead if unset
export TELEGRAM_CHAT_ID='...'
python3 check_slots.py
```

`CUTOFF_DATE=2026-09-01 python3 check_slots.py` forces matches (everything
before September) — handy for testing notifications end to end.
