# Driving Test Slot Watcher — Skåne

Checks Trafikverket for körprov B slots (both manual and automatic
transmission) across 9 Skåne locations.
Sends a Telegram notification when a slot appears before the cutoff date
(default `2026-10-17`, i.e. up to and including Oct 16). The latest full snapshot is always in [REPORT.md](REPORT.md).

## How it works

- `check_slots.py` polls `fp.trafikverket.se/Boka/occasion-bundles` for the
  locations in `locations.json`, 3s between requests. Each location is checked
  for both transmissions (manual = `vehicleTypeId` 2, automatic = 4), so the
  report and notifications label which is which.
- Requests are batched: the API accepts one anchor + up to 3 nearby
  locations at once, so 9 locations normally take ~3 requests instead of
  9. The API caps bundles per response and favors earlier slots, so a
  location whose only availability is far in the future can be crowded out
  of a batch and come back empty. That never hides an *early* slot (those
  sort to the top), but to keep the report honest any location that comes
  back empty is re-queried on its own to confirm. Steady state is ~3
  requests per run.
- Slots before `CUTOFF_DATE` trigger one Telegram message per *new* slot batch.
  Already-notified slots are tracked in `state.json` — no repeat pings.
- Slots on or after `CUTOFF_DATE` never notify, but show up in `REPORT.md`.
- GitHub Actions runs the check (`.github/workflows/slot-watch.yml`) and
  commits the updated report back to the repo.
- Scheduling: a cron-job.org job ("Trafikverket slot watcher dispatch")
  calls the GitHub `workflow_dispatch` API with a fine-grained PAT,
  every 5 min, 24/7 (runs fail fast while the session is dead). GitHub's own cron stayed in the workflow as
  backup, but proved unreliable in this repo (schedule events silently
  never fired despite kick + workflow rename) — that's why the external
  trigger exists.
- The repo is **public** so Actions minutes are unlimited (private repos
  get a 2000 min/month free cap, which running every 5 min 24/7 blows
  through — GitHub bills a minimum of 1 minute per run regardless of how
  short the job actually takes). No PII lives in the repo itself: the
  personnummer and session cookie are GitHub Secrets, and the persisted
  cookie store is AES-256 encrypted before being committed.

### Sessions are short-lived (on-demand mode)

Since about 2026-09-28 Trafikverket force-logs-out every session a fixed
~30 minutes after the BankID login (`forcedLogoutAfterMinutes` in the booking
app). Activity no longer extends it, the server no longer rotates
`FpsExternalIdentity`/`LoginValid`, and there is no "extend session" call.
Because BankID can't be automated, the watcher works **on demand**: after you
log in and refresh `TRV_COOKIE` (see the runbook below), it checks every few
minutes until the session ends, then stops until the next refresh.

When the session ends you get one Telegram message (no re-nagging). Expiry is
detected via `{"status": 401}` in the response body (HTTP 200); as a second
safety net, a run where every location and transmission returns zero slots
also fails and alerts once. A refreshed `TRV_COOKIE` always replaces the
stored cookies (the store remembers a hash of the seed it came from).

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

Manual trigger: `gh workflow run slot-watch.yml`, or wait for the cron-job.org
trigger (every 5 min).

## Cookie refresh runbook

The Trafikverket session cookie expires. When it does, you get one Telegram
alert ("session cookie expired") and runs fail until refreshed:

1. Log in at <https://fp.trafikverket.se/Boka/> (BankID).
2. DevTools → Network tab → click any `occasion-bundles` request →
   Request Headers → copy the full `Cookie` value.
3. `gh secret set TRV_COOKIE` and paste it.
4. `gh workflow run slot-watch.yml` to confirm green.

## Local run

```bash
export TRV_COOKIE='...'
export TRV_SSN='...'
export TELEGRAM_BOT_TOKEN='...'   # optional; prints message instead if unset
export TELEGRAM_CHAT_ID='...'
python3 check_slots.py
```

`CUTOFF_DATE=2026-11-01 python3 check_slots.py` forces matches (everything
before November) — handy for testing notifications end to end.
