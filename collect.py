import json
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

URL = "https://togoversikt.no/api/togkart"
LOCATIONS_FILE = "scan_locations.json"
STOPS_FILE = "stops.json"
TRAIN_KIND = "Gt"
OSLO = ZoneInfo("Europe/Oslo")

DELAY = 3.0          # base seconds between requests
JITTER = 1.5         # plus a random 0 to 1.5 s so we don't hammer on a rhythm
MAX_RETRIES = 3


# ---------------------------------------------------------------- helpers

def to_dt(ts):
    """Timestamp -> Oslo datetime. Works whether it's seconds or milliseconds."""
    if ts > 1e11:
        ts /= 1000
    return datetime.fromtimestamp(ts, OSLO)


def diff(a, b):
    """a - b in seconds, or None if either is missing."""
    return a - b if a is not None and b is not None else None


def metrics(stop):
    """The two numbers we care about for each stop, in seconds."""
    return {
        "scheduled_dwell": diff(stop.get("std"), stop.get("sta")),   # planned time standing
        "actual_dwell": diff(stop.get("atd"), stop.get("ata")),      # real time standing
    }


def load_locations():
    """Returns {code: name} and {code: kind} from locations.json."""
    with open(LOCATIONS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    names = {r["code"]: r["name"] for r in raw}
    kinds = {r["code"]: r["kind"] for r in raw}
    return names, kinds


def load_stops():
    if os.path.exists(STOPS_FILE):
        with open(STOPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_stops(stops):
    # Write to a temp file first so a crash mid-save can't corrupt stops.json
    tmp = STOPS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stops, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STOPS_FILE)


# ---------------------------------------------------------------- fetching

def fetch_fares(session, code):
    """GET one station. Retries on rate limits, server errors and network problems."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(URL, params={"location": code}, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.ConnectionError(f"server said {r.status_code}")
            r.raise_for_status()                  # other 4xx: fail straight away
            return r.json().get("Fares", [])
        except requests.HTTPError:
            raise
        except (requests.RequestException, ValueError) as e:
            if attempt == MAX_RETRIES:
                raise requests.RequestException(str(e))
            wait = 10 * attempt
            print(f"      retry {attempt}/{MAX_RETRIES - 1} in {wait}s ({e})")
            time.sleep(wait)


def make_key(train_id, station, sta, ata):
    """One real stop = one train, one station, one day, one scheduled time."""
    ref = to_dt(sta if sta is not None else ata)
    return f"{train_id}|{station}|{ref:%Y-%m-%d}|{ref:%H:%M}"


def collect_station(session, code, names, kinds, all_stops):
    fares = fetch_fares(session, code)
    new = 0
    dupes = 0

    for fare in fares:
        if fare.get("train_kind") != TRAIN_KIND or fare.get("cancel") == "Y":
            continue

        origin, destination = fare.get("origin"), fare.get("destination")

        for stop in fare.get("stops") or []:
            sta, std = stop.get("sta"), stop.get("std")
            ata, atd = stop.get("ata"), stop.get("atd")

            # Only finished stops: the train has arrived AND left.
            # A train still standing there gets picked up on a later run.
            if ata is None or atd is None:
                continue

            station = stop.get("city")
            key = make_key(fare.get("train_id"), station, sta, ata)
            if key in all_stops:
                dupes += 1
                continue

            # Weekday and time of day come from the TIMETABLE, so delays don't smear them
            planned = to_dt(sta if sta is not None else std if std is not None else ata)

            all_stops[key] = {
                "train_id": fare.get("train_id"),
                "train_no": fare.get("train_no"),
                "company": fare.get("company"),
                "origin": names.get(origin, origin),
                "origin_code": origin,
                "destination": names.get(destination, destination),
                "destination_code": destination,
                "station": station,
                "station_name": names.get(station, station),
                "kind": kinds.get(station),
                "activity": stop.get("activity"),
                "track": stop.get("track"),
                "sta": sta, "std": std,
                "eta": stop.get("eta"), "etd": stop.get("etd"),
                "ata": ata, "atd": atd,
                **metrics(stop),
                "date": f"{planned:%Y-%m-%d}",
                "weekday": planned.weekday(),                          # 0 = Monday
                "minute_of_day": planned.hour * 60 + planned.minute,
                "collected_at": datetime.now(OSLO).isoformat(timespec="seconds"),
            }
            new += 1

    return len(fares), new, dupes


# ---------------------------------------------------------------- main

def main():
    names, kinds = load_locations()
    codes = list(names.keys())

    all_stops = load_stops()
    start_count = len(all_stops)
    session = requests.Session()

    est_min = len(codes) * (DELAY + JITTER / 2) / 60
    print(f"Scanning {len(codes)} locations, one every ~{DELAY:.0f}s")
    print(f"Estimated time: ~{est_min:.0f} minutes")
    print(f"Started {datetime.now(OSLO):%Y-%m-%d %H:%M}, {start_count} stops already saved\n")

    failed = []

    for i, code in enumerate(codes, 1):
        name = names[code]
        try:
            n_fares, n_new, n_dupes = collect_station(session, code, names, kinds, all_stops)
            print(f"[{i:>3}/{len(codes)}] {code:<5} {name:<25} "
                  f"{n_fares:>3} fares | {n_new:>3} new | {n_dupes:>3} already counted")
        except requests.RequestException as e:
            failed.append(code)
            print(f"[{i:>3}/{len(codes)}] {code:<5} {name:<25} FAILED: {e}")

        save_stops(all_stops)
        time.sleep(DELAY + random.uniform(0, JITTER))

    print(f"\nDone. {len(all_stops) - start_count} new stops this run, {len(all_stops)} total.")
    if failed:
        print(f"Failed locations (rerun later): {', '.join(failed)}")


if __name__ == "__main__":
    main()
