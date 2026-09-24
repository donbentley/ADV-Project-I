"""Capture Netflix / Prime Video / Apple TV+ catalogs (US, flatrate) from TMDB.

For each platform and media type:
  1. Page through /discover to collect every title ID released in the last 5 years
  2. Fetch details (+ watch/providers, external_ids) for each ID
  3. Label in-house vs third-party from production_companies / networks, or
     (movies) a US release note showing it premiered on the platform
  4. Write rows to data/titles.csv

Usage:  python3 init.py            (API key read from .env or TMDB_API_KEY)
Details are cached in data/cache/, so a re-run resumes where it stopped.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
BASE = "https://api.themoviedb.org/3"
REGION = "US"
MAX_PAGES = 500  # TMDB hard cap per discover query
WORKERS = 8
PREMIERE_WINDOW = 90  # days after first US release that still counts as a platform premiere
# Only titles released (movies) / first aired (TV) in the last 5 years
SINCE = date.today() - timedelta(days=365 * 5)

PLATFORMS = {
    "Netflix": {
        "providers": "8",
        # substrings matched (case-insensitive) against company / network names
        "in_house": ["netflix"],
        # substrings matched against US release-date notes (movie premieres)
        "premiere": ["netflix"],
    },
    "Amazon Prime Video": {
        "providers": "9|119",
        "in_house": ["amazon studios", "amazon mgm studios", "amazon prime video",
                     "amazon content services", "amazon"],
        "premiere": ["amazon", "prime video"],
    },
    "Apple TV+": {
        "providers": "350",
        "in_house": ["apple tv+", "apple tv", "apple studios", "apple original films"],
        "premiere": ["apple"],
    },
}
MEDIA_TYPES = ["movie", "tv"]


def load_key():
    key = os.environ.get("TMDB_API_KEY")
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TMDB_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("Set TMDB_API_KEY in .env or the environment.")
    return key


API_KEY = load_key()


def get(path, **params):
    params["api_key"] = API_KEY
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except OSError:  # URLError, socket.timeout, connection resets
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed after retries: {path}")


# ---------- Step 1: discover IDs ----------

def discover_ids(media, providers):
    date_field = "primary_release_date" if media == "movie" else "first_air_date"
    base = {
        "with_watch_providers": providers,
        "watch_region": REGION,
        "watch_monetization_types": "flatrate",
        "sort_by": "popularity.desc",
        f"{date_field}.gte": SINCE.isoformat(),
    }
    ids = set()

    def crawl(params):
        first = get(f"/discover/{media}", page=1, **params)
        pages = first["total_pages"]
        for r in first["results"]:
            ids.add(r["id"])
        for p in range(2, min(pages, MAX_PAGES) + 1):
            for r in get(f"/discover/{media}", page=p, **params)["results"]:
                ids.add(r["id"])

    def fits(params):
        return get(f"/discover/{media}", page=1, **params)["total_pages"] <= MAX_PAGES

    if fits(base):
        crawl(base)
    else:
        # Too many results: split by release date until each slice fits under the cap.
        def split(lo, hi):
            params = dict(base, **{f"{date_field}.gte": lo.isoformat(),
                                   f"{date_field}.lte": hi.isoformat()})
            if lo == hi or fits(params):
                crawl(params)
            else:
                mid = lo + (hi - lo) / 2
                split(lo, mid)
                split(mid + timedelta(days=1), hi)

        split(SINCE, date.today() + timedelta(days=365 * 3))
    return ids


# ---------- Step 2: details (cached) ----------

def details(media, tmdb_id):
    path = CACHE / media / f"{tmdb_id}.json"
    extra = "watch/providers,external_ids" + (",release_dates" if media == "movie" else "")
    try:
        if path.exists():
            d = json.loads(path.read_text())
            if media == "movie" and "release_dates" not in d:  # cached before release_dates was added
                d["release_dates"] = get(f"/movie/{tmdb_id}/release_dates") or {"results": []}
                path.write_text(json.dumps(d))
            return d
        d = get(f"/{media}/{tmdb_id}", append_to_response=extra)
    except RuntimeError as e:
        print(f"    skipped: {e}", flush=True)  # re-run later to retry
        return None
    if d is not None:
        path.write_text(json.dumps(d))
    return d


# ---------- Step 3: classify + flatten ----------

def studio_match(d, media, platform):
    names = [c["name"] for c in d.get("production_companies", [])]
    if media == "tv":
        names += [n["name"] for n in d.get("networks", [])]
    keys = PLATFORMS[platform]["in_house"]
    return [n for n in names if any(k in n.lower() for k in keys)]


def platform_premiere(d, platform):
    """True if a US release note names the platform within PREMIERE_WINDOW days of the
    first US release. TMDB often omits the streamer from production_companies on its
    original films (e.g. Red Notice), but notes the streaming premiere here."""
    us = [r for c in d.get("release_dates", {}).get("results", []) if c["iso_3166_1"] == REGION
          for r in c["release_dates"]]
    if not us:
        return False
    first = min(date.fromisoformat(r["release_date"][:10]) for r in us)
    keys = PLATFORMS[platform]["premiere"]
    return any(any(k in (r.get("note") or "").lower() for k in keys)
               and (date.fromisoformat(r["release_date"][:10]) - first).days <= PREMIERE_WINDOW
               for r in us)


def to_row(d, media, platform):
    matched = studio_match(d, media, platform)
    premiere = media == "movie" and platform_premiere(d, platform)
    label = "in_house" if matched or premiere else "third_party"
    us = d.get("watch/providers", {}).get("results", {}).get(REGION, {})
    return {
        "platform": platform,
        "media_type": media,
        "tmdb_id": d["id"],
        "imdb_id": d.get("external_ids", {}).get("imdb_id") or d.get("imdb_id"),
        "title": d.get("title") or d.get("name"),
        "original_language": d.get("original_language"),
        "release_date": d.get("release_date") or d.get("first_air_date"),
        "label": label,
        "matched_on": "; ".join(matched),
        "studio_match": bool(matched),
        "platform_premiere": premiere,
        "production_companies": "; ".join(c["name"] for c in d.get("production_companies", [])),
        "networks": "; ".join(n["name"] for n in d.get("networks", [])),
        "genres": "; ".join(g["name"] for g in d.get("genres", [])),
        "runtime": d.get("runtime") or (d.get("episode_run_time") or [None])[0],
        "number_of_seasons": d.get("number_of_seasons"),
        "number_of_episodes": d.get("number_of_episodes"),
        "status": d.get("status"),
        "vote_average": d.get("vote_average"),
        "vote_count": d.get("vote_count"),
        "popularity": d.get("popularity"),
        "budget": d.get("budget"),
        "revenue": d.get("revenue"),
        "us_flatrate_providers": "; ".join(p["provider_name"] for p in us.get("flatrate", [])),
    }


# ---------- Step 4: run ----------

def main():
    for m in MEDIA_TYPES:
        (CACHE / m).mkdir(parents=True, exist_ok=True)

    rows = []
    for platform, cfg in PLATFORMS.items():
        for media in MEDIA_TYPES:
            ids_file = CACHE / f"ids_{platform.replace(' ', '_').replace('+', 'plus')}_{media}.json"
            if ids_file.exists():
                ids = json.loads(ids_file.read_text())
            else:
                print(f"[{platform} / {media}] discovering...", flush=True)
                ids = sorted(discover_ids(media, cfg["providers"]))
                ids_file.write_text(json.dumps(ids))
            print(f"[{platform} / {media}] {len(ids)} titles, fetching details...", flush=True)

            done = 0
            with ThreadPoolExecutor(WORKERS) as pool:
                for d in pool.map(lambda i: details(media, i), ids):
                    done += 1
                    if d:
                        rows.append(to_row(d, media, platform))
                    if done % 1000 == 0:
                        print(f"    {done}/{len(ids)}", flush=True)

    out = DATA / "titles.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
