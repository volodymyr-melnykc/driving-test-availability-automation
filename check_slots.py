#!/usr/bin/env python3
"""Check Trafikverket driving test (körprov B) slot availability in Skåne.

Polls the occasion-bundles API for each location in locations.json,
notifies via Telegram about new slots before CUTOFF_DATE, and writes
a full availability snapshot to REPORT.md. Stdlib only.

Session handling: Trafikverket sessions live 30 minutes (sliding) and the
FpsExternalIdentity token rotates on every response. The script therefore
honors Set-Cookie headers and persists the rotated cookies to
COOKIE_STORE_FILE so the next run (<=20 min later) continues the session.
The TRV_COOKIE env var seeds the store and takes precedence whenever its
LoginValid timestamp is newer than the store's (i.e. after a fresh login).

Required env vars:
    TRV_COOKIE          full Cookie header value from a logged-in browser session
    TRV_SSN             personnummer used for the booking session
    TELEGRAM_BOT_TOKEN  bot token from @BotFather (optional: skip notifications)
    TELEGRAM_CHAT_ID    chat id to notify (optional: skip notifications)
Optional:
    CUTOFF_DATE         notify only for slots strictly before this date (default 2026-08-13)
    COOKIE_STORE_FILE   path for the persisted cookie store (default cookie_store.json)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
REPORT_FILE = BASE_DIR / "REPORT.md"
LOCATIONS_FILE = BASE_DIR / "locations.json"

API_URL = "https://fp.trafikverket.se/Boka/occasion-bundles"
BOOKING_URL = "https://fp.trafikverket.se/Boka/ng/search/CORrMCLoCsPaRp/5/12/0/0"
REQUEST_DELAY_SECONDS = 3
MAX_SEEN_KEYS = 2000
RE_ALERT_HOURS = 6  # re-nag this often while the session stays expired
# The API accepts one anchor + up to 3 nearby locations per request (the same
# 4-location cap the booking UI enforces). Extra nearby ids are silently
# dropped, so we batch our locations in groups of this size to cut the request
# count per run from 11 to 3.
LOCATIONS_PER_REQUEST = 4

# Körprov B comes in two transmissions, distinguished by vehicleTypeId. We
# check both. Each has its own booking deep-link (label, vehicleTypeId, url).
VEHICLE_TYPES = [
    ("Manual", 2, "https://fp.trafikverket.se/Boka/ng/search/CORrMCLoCsPaRp/5/12/0/0"),
    ("Automatic", 4, "https://fp.trafikverket.se/Boka/ng/search/PRLLePIrPydClD/5/0/0/0"),
]


class LoginRequired(Exception):
    pass


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "last_cookie_alert": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def parse_cookie_string(cookie_str):
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k] = v
    return cookies


def login_valid_ts(cookies):
    """LoginValid cookie as comparable string, e.g. '2026-06-10 15:29'."""
    return cookies.get("LoginValid", "")


def load_cookie_store(store_file, env_cookie):
    """Prefer the persisted (rotated) store unless the env cookie is newer."""
    env_cookies = parse_cookie_string(env_cookie)
    if store_file.exists():
        stored = json.loads(store_file.read_text())
        if login_valid_ts(stored) >= login_valid_ts(env_cookies):
            return stored
    return env_cookies


def apply_set_cookies(cookies, headers):
    for sc in headers.get_all("Set-Cookie") or []:
        first = sc.split(";", 1)[0]
        if "=" in first:
            k, v = first.split("=", 1)
            cookies[k] = v


def fetch_batch(cookies, ssn, location_ids, vehicle_type_id):
    """Query up to LOCATIONS_PER_REQUEST locations in one request.

    The first id is the anchor (locationId), the rest are nearbyLocationIds.
    vehicle_type_id selects transmission (2 = manual, 4 = automatic).
    Returns {location_id: sorted occasions}; rotates cookies in place.
    """
    anchor, nearby = location_ids[0], location_ids[1:]
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
            "locationId": anchor,
            "nearbyLocationIds": nearby,
            "languageId": 0,
            "vehicleTypeId": vehicle_type_id,
            "tachographTypeId": 1,
            "occasionChoiceId": 1,
            "examinationTypeId": 12,
        },
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=UTF-8",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
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
        apply_set_cookies(cookies, resp.headers)
        body = json.loads(resp.read().decode())

    if body.get("type") == "LoginRequiredException" or (
        isinstance(body.get("data"), dict) and body["data"].get("success") is False
    ):
        raise LoginRequired(body.get("data", {}).get("message", "login required"))

    # dedupe occasions (the API repeats them) and split by location
    by_location = {loc_id: {} for loc_id in location_ids}
    for bundle in body.get("data", {}).get("bundles", []):
        for occ in bundle.get("occasions", []):
            loc_id = occ["locationId"]
            by_location.setdefault(loc_id, {})[(occ["date"], occ["time"])] = occ
    return {
        loc_id: sorted(occ_map.values(), key=lambda o: (o["date"], o["time"]))
        for loc_id, occ_map in by_location.items()
    }


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


def write_report(results, errors, cutoff, session_valid, locations):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Driving Test Availability — Skåne",
        "",
        f"Last checked: {now}  ",
        f"Session valid until: {session_valid or 'unknown'} (Swedish local time)  ",
        f"Notification cutoff: slots before **{cutoff}**",
        "",
        "| Location | Transmission | Earliest slots | Cost |",
        "|---|---|---|---|",
    ]
    for name in locations:
        for transmission, _, _ in VEHICLE_TYPES:
            occasions = results.get((name, transmission), [])
            if occasions:
                earliest = occasions[:3]
                slots = "<br>".join(f"{o['date']} {o['time']}" for o in earliest)
                cost = earliest[0].get("cost", "")
            else:
                slots, cost = "no slots found", ""
            lines.append(f"| {name} | {transmission} | {slots} | {cost} |")
    if errors:
        lines += ["", "## Errors", ""]
        lines += [f"- {name}: {err}" for name, err in errors.items()]
    lines += ["", f"[Book on Trafikverket]({BOOKING_URL})", ""]
    REPORT_FILE.write_text("\n".join(lines))


def collect_availability(cookies, ssn, locations):
    """Fetch occasions for every location and transmission, batching requests.

    Each transmission (manual / automatic) is queried separately because it's
    a different vehicleTypeId. Within a transmission, locations are batched
    4-at-a-time. The API caps bundles per response and prioritizes earlier
    slots, so a location whose only availability is far in the future can be
    crowded out of a batch and come back empty. That never hides an *early*
    slot (those sort to the top), but it makes "empty" ambiguous — so any
    (location, transmission) that returns nothing in its batch is re-queried
    on its own to confirm. Returns ({(name, transmission): sorted occasions},
    {label: error}). Raises LoginRequired.
    """
    name_by_id = {loc_id: name for name, loc_id in locations.items()}
    ids = list(locations.values())
    batches = [
        ids[i : i + LOCATIONS_PER_REQUEST]
        for i in range(0, len(ids), LOCATIONS_PER_REQUEST)
    ]

    results = {}
    errors = {}
    request_count = 0

    def run_query(query_ids, vehicle_type_id, transmission, label):
        nonlocal request_count
        if request_count > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        request_count += 1
        try:
            batch_res = fetch_batch(cookies, ssn, query_ids, vehicle_type_id)
            for loc_id, occasions in batch_res.items():
                name = name_by_id.get(loc_id)
                if name is not None and occasions:
                    results[(name, transmission)] = occasions
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
            errors[label] = str(e)
            print(f"{label}: ERROR {e}", file=sys.stderr)
            return False

    for transmission, vehicle_type_id, _ in VEHICLE_TYPES:
        for batch in batches:
            label = f"{transmission}: " + "/".join(name_by_id[x] for x in batch)
            run_query(batch, vehicle_type_id, transmission, label)
        # Re-query, on their own, any location left empty for this transmission
        # — could be genuinely empty or crowded out of its batch.
        empty = [n for n in locations if (n, transmission) not in results]
        for name in empty:
            run_query([locations[name]], vehicle_type_id, transmission,
                      f"{transmission}: {name}")

    for transmission, _, _ in VEHICLE_TYPES:
        for name in locations:
            occ = results.get((name, transmission), [])
            earliest = occ[0]["date"] if occ else "none"
            print(f"{transmission} {name}: {len(occ)} slots, earliest {earliest}")
    print(f"({request_count} requests for {len(locations)} locations × "
          f"{len(VEHICLE_TYPES)} transmissions)")
    return results, errors


def main():
    env_cookie = os.environ.get("TRV_COOKIE")
    ssn = os.environ.get("TRV_SSN")
    if not env_cookie or not ssn:
        print("ERROR: TRV_COOKIE and TRV_SSN env vars are required", file=sys.stderr)
        return 2
    cutoff = os.environ.get("CUTOFF_DATE", "2026-08-13")
    store_file = Path(os.environ.get("COOKIE_STORE_FILE", BASE_DIR / "cookie_store.json"))

    cookies = load_cookie_store(store_file, env_cookie)
    print(f"Session LoginValid: {login_valid_ts(cookies) or 'unknown'}")

    locations = json.loads(LOCATIONS_FILE.read_text())

    state = load_state()
    state.pop("cookie_alert_sent", None)  # migrate from the old one-shot flag
    try:
        results, errors = collect_availability(cookies, ssn, locations)
        login_failed = False
    except LoginRequired as e:
        print(f"LOGIN REQUIRED ({e})", file=sys.stderr)
        results, errors, login_failed = {}, {}, True

    if login_failed:
        # Do NOT persist on a hard login failure: the rotated cookies are dead,
        # and overwriting the store would also clobber a still-newer env seed.
        # Re-nag every RE_ALERT_HOURS so an expired session can't go silently
        # unnoticed for days — recovery needs a manual BankID login.
        now = datetime.now(timezone.utc)
        last_alert = state.get("last_cookie_alert")
        due = last_alert is None or (
            now - datetime.fromisoformat(last_alert)
        ) >= timedelta(hours=RE_ALERT_HOURS)
        if due:
            send_telegram(
                "⚠️ Trafikverket slot checker: session expired.\n"
                "Log in at https://fp.trafikverket.se/Boka/, copy the Cookie "
                "header from DevTools, then run:\n"
                "gh secret set TRV_COOKIE"
            )
            state["last_cookie_alert"] = now.isoformat()
            save_state(state)
        return 1

    # persist rotated cookies (success or recoverable per-location error) so the
    # sliding session continues on the next run
    store_file.write_text(json.dumps(cookies, indent=1) + "\n")
    state["last_cookie_alert"] = None

    booking_url = {label: url for label, _, url in VEHICLE_TYPES}
    today = date.today().isoformat()
    seen = set(state.get("seen", []))
    new_matches = []
    for (name, transmission), occasions in results.items():
        for occ in occasions:
            if occ["date"] >= cutoff:
                continue
            key = f"{name}|{transmission}|{occ['date']}|{occ['time']}"
            if key not in seen:
                new_matches.append((name, transmission, occ))
                seen.add(key)

    if new_matches:
        new_matches.sort(key=lambda m: (m[2]["date"], m[2]["time"], m[0]))
        lines = [f"🚗 Early driving test slots found (before {cutoff}):", ""]
        lines += [
            f"• {occ['date']} {occ['time']} — {name} [{transmission}]"
            f" ({occ.get('cost', '?')})"
            for name, transmission, occ in new_matches
        ]
        transmissions = {m[1] for m in new_matches}
        lines += [""] + [f"Book {t}: {booking_url[t]}" for t in sorted(transmissions)]
        send_telegram("\n".join(lines))
        print(f"Notified about {len(new_matches)} new slot(s)")
    else:
        print("No new slots before cutoff")

    # prune keys for past dates, cap total size (key: name|transmission|date|time)
    seen = sorted(k for k in seen if k.split("|")[2:3] >= [today])[-MAX_SEEN_KEYS:]
    state["seen"] = seen
    save_state(state)
    write_report(results, errors, cutoff, login_valid_ts(cookies), locations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
