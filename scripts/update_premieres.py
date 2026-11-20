
"""Audit upcoming US movie premieres.

Uses Galaxy Movies for upcoming arrivals and optional
MDBList metadata for release-date checks.

This script creates an audit, not a verified Kodi widget.
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

FEED_URL = (
    "https://www.galaxymovies.app/api/"
    "streaming-changes?provider=all"
)

API_KEY = os.environ.get("MDBLIST_API_KEY", "")

TARGET_SERVICES = {
    "netflix",
    "amazon-prime-video",
    "apple-tv",
    "max",
    "disney-plus",
    "hulu",
    "peacock",
    "paramount-plus",
}

SUBSCRIPTION_SERVICES = TARGET_SERVICES | {
    "starz",
    "amc-plus",
    "mgm-plus",
    "crunchyroll",
    "discovery-plus",
    "shudder",
    "criterion",
}

# Known specials that should not appear as feature movies
KNOWN_SPECIALS = {
    1827764: "Family Guy TV special",
    1818957: "Stand-up comedy special",
}

OUTPUT_DIR = Path("charts")

HISTORY_PATH = (
    OUTPUT_DIR / "premieres_seen_on_streaming.json"
)

AUDIT_PATH = (
    OUTPUT_DIR / "premieres_audit.json"
)

LOOKAHEAD_DAYS = 120


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KodiPremieresAudit/2.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        return json.load(response)


def valid_movie_id(item):
    if not isinstance(item, dict):
        return None

    if item.get("tmdbType") != "movie":
        return None

    try:
        movie_id = int(item.get("tmdbId"))
        return movie_id if movie_id > 0 else None
    except (TypeError, ValueError):
        return None


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def movie_metadata(tmdb_id):
    if not API_KEY:
        return None

    url = (
        f"https://api.mdblist.com/tmdb/movie/{tmdb_id}?"
        + urllib.parse.urlencode({"apikey": API_KEY})
    )

    try:
        result = fetch_json(url)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
    ):
        # Never log URLs containing the private API key
        return None

    if (
        isinstance(result, dict)
        and result.get("type") == "movie"
    ):
        return result

    return None


def classify(record, observed, metadata):
    movie_id = record["tmdb_id"]

    streaming_date = parse_date(
        record["earliest_streaming_date"]
    )

    if movie_id in KNOWN_SPECIALS:
        return (
            "exclude_nonfeature_special",
            KNOWN_SPECIALS[movie_id],
        )

    if observed:
        return (
            "possible_transfer",
            "Previously observed in a provider change feed",
        )

    if not metadata:
        return (
            "needs_metadata",
            "Metadata unavailable; no premiere verification",
        )

    runtime = metadata.get("runtime")

    if (
        isinstance(runtime, (int, float))
        and 0 < runtime < 50
    ):
        return (
            "short_or_special_review",
            "Under 50 minutes; may be a special",
        )

    first_release = parse_date(
        metadata.get("released")
    )

    first_digital = parse_date(
        metadata.get("released_digital")
    )

    if (
        first_release
        and (streaming_date - first_release).days > 540
    ):
        return (
            "older_release_unverified",
            "First known release over 18 months ago",
        )

    if (
        first_digital
        and (streaming_date - first_digital).days > 365
    ):
        return (
            "older_digital_unverified",
            "Digital release over one year ago",
        )

    return (
        "potential_premiere_unverified",
        "Recent release with no observed transfer; "
        "not proof of first streaming",
    )


def main():
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    payload = fetch_json(FEED_URL)

    providers = (
        payload.get("providers")
        if isinstance(payload, dict)
        else None
    )

    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            "Galaxy Movies feed did not contain providers"
        )

    if HISTORY_PATH.exists():
        history = json.loads(
            HISTORY_PATH.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(history, dict):
            raise ValueError(
                "Unexpected previous-history format"
            )
    else:
        history = {}

    today = datetime.now(timezone.utc).date()

    cutoff = today + timedelta(
        days=LOOKAHEAD_DAYS
    )

    # Record observed arrivals and departures
    for provider, groups in providers.items():
        if provider not in SUBSCRIPTION_SERVICES:
            continue

        if not isinstance(groups, dict):
            continue

        for name in ("added", "left", "leavingSoon"):
            for item in groups.get(name) or []:
                movie_id = valid_movie_id(item)

                if movie_id is not None:
                    services = history.setdefault(
                        str(movie_id), []
                    )

                    if provider not in services:
                        services.append(provider)

    # Collect upcoming movies
    candidates = {}

    for provider, groups in providers.items():
        if provider not in TARGET_SERVICES:
            continue

        if not isinstance(groups, dict):
            continue

        for item in groups.get("coming") or []:
            movie_id = valid_movie_id(item)

            arrival = (
                parse_date(item.get("date"))
                if isinstance(item, dict)
                else None
            )

            if movie_id is None or arrival is None:
                continue

            if not today <= arrival <= cutoff:
                continue

            record = candidates.setdefault(
                str(movie_id),
                {
                    "tmdb_id": movie_id,
                    "title": str(
                        item.get("title") or "Unknown"
                    ),
                    "streaming_dates": {},
                },
            )

            record["streaming_dates"][
                provider
            ] = arrival.isoformat()

    # Classify upcoming movie candidates
    results = []

    for movie_id, record in candidates.items():
        record["earliest_streaming_date"] = min(
            record["streaming_dates"].values()
        )

        observed = sorted(
            history.get(movie_id, [])
        )

        record[
            "observed_previous_services"
        ] = observed

        metadata = movie_metadata(
            int(movie_id)
        )

        if metadata:
            record["metadata"] = {
                "year": metadata.get("year"),
                "released": metadata.get("released"),
                "released_digital": metadata.get(
                    "released_digital"
                ),
                "runtime": metadata.get("runtime"),
            }

        status, reason = classify(
            record,
            observed,
            metadata,
        )

        record["status"] = status
        record["reason"] = reason

        results.append(record)

        if API_KEY:
            time.sleep(0.25)

    results.sort(
        key=lambda record: (
            record["earliest_streaming_date"],
            record["title"],
        )
    )

    audit = {
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": (
            "Powered by Galaxy Movies - "
            "https://www.galaxymovies.app"
        ),
        "metadata_source": (
            "MDBList when API key configured"
        ),
        "purpose": (
            "Audit only; no confirmed first-time "
            "premieres or Kodi widget"
        ),
        "limitation": (
            "Release dates and observed platform "
            "changes are not complete historical "
            "subscription-streaming records."
        ),
        "metadata_configured": bool(API_KEY),
        "counts": dict(
            Counter(
                record["status"]
                for record in results
            )
        ),
        "candidates": results,
    }

    HISTORY_PATH.write_text(
        json.dumps(
            history,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    AUDIT_PATH.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        f"Audited {len(results)} movie candidates "
        f"from {len(providers)} providers"
    )

    print(
        f"MDBList metadata enabled: {bool(API_KEY)}"
    )

    print(
        f"Results by status: {audit['counts']}"
    )

    print(
        "Audit only: no Kodi widget generated"
    )


if __name__ == "__main__":
    main()
