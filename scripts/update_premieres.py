"""Audit upcoming US subscription-streaming movie arrivals.

Does not prove a first-ever streaming premiere. No Kodi widget output.
Galaxy Movies attribution: https://www.galaxymovies.app
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

GALAXY_URL = (
    "https://www.galaxymovies.app/api/"
    "streaming-changes?provider=all"
)
API_KEY = os.environ.get("MDBLIST_API_KEY", "")
SERVICES = {
    "netflix", "amazon-prime-video", "apple-tv", "max",
    "disney-plus", "hulu", "peacock", "paramount-plus",
}
HISTORY_SERVICES = SERVICES | {
    "starz", "amc-plus", "mgm-plus", "crunchyroll",
    "discovery-plus", "shudder", "criterion",
}
KNOWN_SPECIALS = {
    1827764: "Family Guy TV special",
    1818957: "Stand-up comedy special",
}
OUTPUT = Path("charts")
HISTORY = OUTPUT / "premieres_seen_on_streaming.json"
AUDIT = OUTPUT / "premieres_audit.json"
DAYS_AHEAD = 120


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KodiPremieresAudit/3.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def movie_id(item):
    if not isinstance(item, dict) or item.get("tmdbType") != "movie":
        return None
    try:
        value = int(item.get("tmdbId"))
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def get_metadata(tmdb_id):
    if not API_KEY:
        return None
    url = (
        f"https://api.mdblist.com/tmdb/movie/{tmdb_id}?"
        + urllib.parse.urlencode({"apikey": API_KEY})
    )
    for attempt in range(3):
        try:
            result = fetch_json(url)
            return result if (
                isinstance(result, dict)
                and result.get("type") == "movie"
            ) else None
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError, ValueError) as error:
            # Never print the request URL: it contains the private API key.
            if isinstance(error, urllib.error.HTTPError):
                if error.code not in (429, 500, 502, 503, 504):
                    break
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None


def classify(record, observed, metadata):
    movie = record["tmdb_id"]
    arrival = parse_date(record["earliest_streaming_date"])
    flags = []

    if movie in KNOWN_SPECIALS:
        return "exclude_nonfeature_special", [KNOWN_SPECIALS[movie]]

    if observed:
        flags.append(
            "Recorded streaming arrival/departure on: "
            + ", ".join(observed)
        )

    if not metadata:
        flags.append("MDBList metadata unavailable")
        return (
            "possible_transfer" if observed else "needs_metadata",
            flags,
        )

    released = parse_date(metadata.get("released"))
    digital = parse_date(metadata.get("released_digital"))
    runtime = metadata.get("runtime")

    # A source date well after its supposed streaming arrival is
    # contradictory, but may reflect inaccurate metadata.
    if released and released > arrival + timedelta(days=30):
        flags.append("Theatrical/general release is after streaming arrival")
    if digital and digital > arrival + timedelta(days=30):
        flags.append("Digital release is after streaming arrival")

    if isinstance(runtime, (float, int)) and 0 < runtime < 50:
        flags.append("Runtime under 50 minutes: possible special or short")

    # Older dates are review signals, not evidence of a transfer.
    if digital and (arrival - digital).days > 365:
        flags.append("Digital release was more than one year earlier")
    if released and (arrival - released).days > 540:
        flags.append("General release was more than 18 months earlier")

    if any("after streaming arrival" in f for f in flags):
        status = "release_date_conflict"
    elif observed:
        status = "possible_transfer"
    elif any("possible special or short" in f for f in flags):
        status = "short_or_special_review"
    elif any("earlier" in f for f in flags):
        status = "older_release_unverified"
    else:
        status = "potential_premiere_unverified"

    return status, flags or [
        "Recent release; no transfer observed in incomplete history"
    ]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    feed = fetch_json(GALAXY_URL)
    providers = feed.get("providers") if isinstance(feed, dict) else None
    if not isinstance(providers, dict) or not providers:
        raise ValueError("Galaxy Movies returned no usable providers")

    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {}
    if not isinstance(history, dict):
        raise ValueError("Previous streaming-history file is not a dictionary")

    now = datetime.now(timezone.utc)
    today = now.date()
    end = today + timedelta(days=DAYS_AHEAD)

    # Continue accumulating observed changes. Not a complete history.
    for provider, groups in providers.items():
        if provider not in HISTORY_SERVICES or not isinstance(groups, dict):
            continue
        for group in ("added", "left", "leavingSoon"):
            for item in groups.get(group) or []:
                ident = movie_id(item)
                if ident is None:
                    continue
                seen = history.setdefault(str(ident), [])
                if provider not in seen:
                    seen.append(provider)

    candidates = {}
    for provider, groups in providers.items():
        if provider not in SERVICES or not isinstance(groups, dict):
            continue
        for item in groups.get("coming") or []:
            ident = movie_id(item)
            arrival = parse_date(item.get("date")) if isinstance(item, dict) else None
            if ident is None or arrival is None or not today <= arrival <= end:
                continue
            record = candidates.setdefault(str(ident), {
                "tmdb_id": ident,
                "title": str(item.get("title") or "Unknown"),
                "streaming_dates": {},
            })
            record["streaming_dates"][provider] = arrival.isoformat()

    results = []
    for ident, record in candidates.items():
        record["earliest_streaming_date"] = min(record["streaming_dates"].values())
        seen = sorted(history.get(ident, []))
        record["observed_previous_services"] = seen
        metadata = get_metadata(int(ident))
        if metadata:
            record["metadata"] = {
                field: metadata.get(field)
                for field in ("year", "released", "released_digital", "runtime")
            }
        status, flags = classify(record, seen, metadata)
        record["status"] = status
        record["reasons"] = flags
        results.append(record)
        if API_KEY:
            time.sleep(0.25)

    results.sort(key=lambda r: (r["earliest_streaming_date"], r["title"]))
    audit = {
        "generated_at_utc": now.isoformat(),
        "purpose": "Diagnostic audit only; no verified Kodi widget",
        "source": "Powered by Galaxy Movies - https://www.galaxymovies.app",
        "metadata_source": "MDBList",
        "metadata_configured": bool(API_KEY),
        "limitation": (
            "The provider-change history starts when this job was installed; "
            "it cannot prove a movie has never streamed anywhere before. "
            "Older digital dates do not alone imply subscription transfers."
        ),
        "counts": dict(Counter(r["status"] for r in results)),
        "candidates": results,
    }
    HISTORY.write_text(
        json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    AUDIT.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Audited {len(results)} movies from {len(providers)} providers")
    print(f"Results by status: {audit['counts']}")
    print("No Kodi widget generated; premiere status is unverified")


if __name__ == "__main__":
    main()
