#!/usr/bin/env python3
"""Check Trafikverket driving test (körprov B) slot availability in Skåne.

Polls the occasion-bundles API for each location in locations.json,
notifies via Telegram about new slots before CUTOFF_DATE, and writes
a full availability snapshot to REPORT.md. Stdlib only.

Required env vars:
    TRV_COOKIE          full Cookie header value from a logged-in browser session
    TRV_SSN             personnummer used for the booking session
    TELEGRAM_BOT_TOKEN  bot token from @BotFather (optional: skip notifications)
    TELEGRAM_CHAT_ID    chat id to notify (optional: skip notifications)
Optional:
    CUTOFF_DATE         notify only for slots strictly before this date (default 2026-07-15)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
REPORT_FILE = BASE_DIR / "REPORT.md"
LOCATIONS_FILE = BASE_DIR / "locations.json"

API_URL = "https://fp.trafikverket.se/Boka/occasion-bundles"
BOOKING_URL = "https://fp.trafikverket.se/Boka/ng/search/CORrMCLoCsPaRp/5/12/0/0"
REQUEST_DELAY_SECONDS = 3
MAX_SEEN_KEYS = 2000


class LoginRequired(Exception):
    pass


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "cookie_alert_sent": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def fetch_location(cookie, ssn, location_id):
    """Return deduped list of occasions for one location."""
    payload = {
        "bookingSession": {
            "socialSecurityNumber": ssn,
            "licenceId": 5,
            "bookingModeId": 0,
            "ignoreDebt": False,
            "ignoreBookingHindrance": False,
            "examinationTypeId": 12,
            "excludeExaminationCategories": [],
            "rescheduleTypeId": 0,
            "paymentIsActive": False,
            "paymentReference": "",
            "paymentUrl": "",
            "searchedMonths": 0,
        },
        "occasionBundleQuery": {
            "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z"),
            "searchedMonths": 0,
            "locationId": location_id,
            "nearbyLocationIds": [],
            "languageId": 0,
            "vehicleTypeId": 2,
            "tachographTypeId": 1,
            "occasionChoiceId": 1,
            "examinationTypeId": 12,
        },
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://fp.trafikverket.se",
        "Referer": BOOKING_URL,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())

    if body.get("type") == "LoginRequiredException" or (
        isinstance(body.get("data"), dict) and body["data"].get("success") is False
    ):
        raise LoginRequired(body.get("data", {}).get("message", "login required"))

    occasions = {}
    for bundle in body.get("data", {}).get("bundles", []):
        for occ in bundle.get("occasions", []):
            key = (occ["locationId"], occ["date"], occ["time"])
            occasions[key] = occ
    return sorted(occasions.values(), key=lambda o: (o["date"], o["time"]))


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured; would have sent:\n" + text)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def write_report(results, errors, cutoff):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Driving Test Availability — Skåne",
        "",
        f"Last checked: {now}  ",
        f"Notification cutoff: slots before **{cutoff}**",
        "",
        "| Location | Earliest slots | Cost |",
        "|---|---|---|",
    ]
    for name, occasions in results.items():
        if occasions:
            earliest = occasions[:3]
            slots = "<br>".join(f"{o['date']} {o['time']}" for o in earliest)
            cost = earliest[0].get("cost", "")
        else:
            slots, cost = "no slots found", ""
        lines.append(f"| {name} | {slots} | {cost} |")
    if errors:
        lines += ["", "## Errors", ""]
        lines += [f"- {name}: {err}" for name, err in errors.items()]
    lines += ["", f"[Book on Trafikverket]({BOOKING_URL})", ""]
    REPORT_FILE.write_text("\n".join(lines))


def main():
    cookie = os.environ.get("TRV_COOKIE")
    ssn = os.environ.get("TRV_SSN")
    if not cookie or not ssn:
        print("ERROR: TRV_COOKIE and TRV_SSN env vars are required", file=sys.stderr)
        return 2
    cutoff = os.environ.get("CUTOFF_DATE", "2026-07-15")

    locations = json.loads(LOCATIONS_FILE.read_text())
    state = load_state()
    results, errors = {}, {}
    login_failed = False

    for i, (name, location_id) in enumerate(locations.items()):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            results[name] = fetch_location(cookie, ssn, location_id)
            earliest = results[name][0]["date"] if results[name] else "none"
            print(f"{name}: {len(results[name])} slots, earliest {earliest}")
        except LoginRequired as e:
            print(f"{name}: LOGIN REQUIRED ({e})", file=sys.stderr)
            login_failed = True
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
            errors[name] = str(e)
            results[name] = []
            print(f"{name}: ERROR {e}", file=sys.stderr)

    if login_failed:
        if not state.get("cookie_alert_sent"):
            send_telegram(
                "⚠️ Trafikverket slot checker: session cookie expired.\n"
                "Log in at https://fp.trafikverket.se/Boka/, copy the Cookie "
                "header from DevTools, then run:\n"
                "gh secret set TRV_COOKIE"
            )
            state["cookie_alert_sent"] = True
            save_state(state)
        return 1
    state["cookie_alert_sent"] = False

    today = date.today().isoformat()
    seen = set(state.get("seen", []))
    new_matches = []
    for name, occasions in results.items():
        for occ in occasions:
            if occ["date"] >= cutoff:
                continue
            key = f"{name}|{occ['date']}|{occ['time']}"
            if key not in seen:
                new_matches.append((name, occ))
                seen.add(key)

    if new_matches:
        new_matches.sort(key=lambda m: (m[1]["date"], m[1]["time"]))
        lines = [f"🚗 Early driving test slots found (before {cutoff}):", ""]
        lines += [
            f"• {occ['date']} {occ['time']} — {name} ({occ.get('cost', '?')})"
            for name, occ in new_matches
        ]
        lines += ["", f"Book now: {BOOKING_URL}"]
        send_telegram("\n".join(lines))
        print(f"Notified about {len(new_matches)} new slot(s)")
    else:
        print("No new slots before cutoff")

    # prune keys for past dates, cap total size
    seen = sorted(k for k in seen if k.split("|")[1] >= today)[-MAX_SEEN_KEYS:]
    state["seen"] = seen
    save_state(state)
    write_report(results, errors, cutoff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
