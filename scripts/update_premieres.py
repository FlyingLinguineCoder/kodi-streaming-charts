
"""Audit upcoming US subscription-streaming movie arrivals.

Data sources: Galaxy Movies catalog changes and MDBList movie metadata.
This audit does NOT establish first-ever subscription-streaming premieres.
No Kodi widget is written. Galaxy Movies: https://www.galaxymovies.app
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

SERVICE_NAMES = {
    "netflix": "Netflix", "amazon-prime-video": "Prime Video",
    "apple-tv": "Apple TV+", "max": "HBO Max", "disney-plus": "Disney+",
    "hulu": "Hulu", "peacock": "Peacock", "paramount-plus": "Paramount+",
}
SERVICES = set(SERVICE_NAMES)

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
ANNOUNCEMENTS_KEY = "_announced_arrivals"

# Match common names in MDBList's watch_providers field.
# These are clues, not definitive proof of current subscriptions.
SERVICE_ALIASES = {
    "netflix": "netflix",
    "netflix basic with ads": "netflix",
    "amazon prime video": "amazon-prime-video",
    "amazon prime": "amazon-prime-video",
    "prime video": "amazon-prime-video",
    "apple tv+": "apple-tv",
    "apple tv plus": "apple-tv",
    "max": "max",
    "hbo max": "max",
    "disney plus": "disney-plus",
    "disney+": "disney-plus",
    "hulu": "hulu",
    "hulu with ads": "hulu",
    "peacock": "peacock",
    "peacock premium": "peacock",
    "peacock premium plus": "peacock",
    "paramount plus": "paramount-plus",
    "paramount+": "paramount-plus",
    "paramount+ with showtime": "paramount-plus",
    "starz": "starz",
    "amc+": "amc-plus",
    "mgm+": "mgm-plus",
    "crunchyroll": "crunchyroll",
    "discovery+": "discovery-plus",
    "shudder": "shudder",
    "criterion channel": "criterion",
}


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KodiPremieresAudit/4.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        return json.load(response)


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def movie_id(item):
    if not isinstance(item, dict):
        return None

    if item.get("tmdbType") != "movie":
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

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
        ) as error:

            # Do not print URLs containing the private API key.
            if isinstance(error, urllib.error.HTTPError):
                if error.code not in (429, 500, 502, 503, 504):
                    break

            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    return None


def known_watch_services(metadata):
    """Find recognized services, not rental-only store names."""

    if not isinstance(metadata, dict):
        return []

    providers = metadata.get("watch_providers") or []

    if not isinstance(providers, list):
        return []

    seen = set()

    for provider in providers:
        if not isinstance(provider, dict):
            continue

        name = str(
            provider.get("name") or ""
        ).strip().lower()

        service = SERVICE_ALIASES.get(name)

        if service:
            seen.add(service)

    return sorted(seen)


def classify(
    record,
    observed,
    metadata,
    earlier_announcements,
):
    ident = record["tmdb_id"]

    arrival = parse_date(
        record["earliest_streaming_date"]
    )

    flags = []

    if ident in KNOWN_SPECIALS:
        return (
            "exclude_nonfeature_special",
            [KNOWN_SPECIALS[ident]],
        )

    if observed:
        flags.append(
            "Earlier provider-change record observed on: "
            + ", ".join(observed)
        )

    if earlier_announcements:
        flags.append(
            "Earlier streaming dates previously announced: "
            + "; ".join(
                f"{service} ({when})"
                for service, when in sorted(
                    earlier_announcements.items()
                )
            )
        )

    current_services = known_watch_services(metadata)

    destination_services = set(
        record["streaming_dates"]
    )

    others = sorted(
        set(current_services) - destination_services
    )

    record["mdblist_watch_providers"] = current_services
    record["other_watch_providers"] = others

    if others:
        flags.append(
            "MDBList also lists other watch providers: "
            + ", ".join(others)
            + " (not proof of past subscription streaming)"
        )

    if not metadata:
        flags.append(
            "MDBList metadata unavailable"
        )

        return (
            "possible_transfer"
            if (observed or earlier_announcements)
            else "needs_metadata",
            flags,
        )

    released = parse_date(
        metadata.get("released")
    )

    digital = parse_date(
        metadata.get("released_digital")
    )

    runtime = metadata.get("runtime")

    if (
        released
        and released > arrival + timedelta(days=30)
    ):
        flags.append(
            "Theatrical/general release is after streaming arrival"
        )

    if (
        digital
        and digital > arrival + timedelta(days=30)
    ):
        flags.append(
            "Digital release is after streaming arrival"
        )

    if (
        isinstance(runtime, (float, int))
        and 0 < runtime < 50
    ):
        flags.append(
            "Runtime under 50 minutes: possible special or short"
        )

    if (
        digital
        and (arrival - digital).days > 365
    ):
        flags.append(
            "Digital release was more than one year earlier"
        )

    if (
        released
        and (arrival - released).days > 540
    ):
        flags.append(
            "General release was more than 18 months earlier"
        )

    if any(
        "after streaming arrival" in flag
        for flag in flags
    ):
        status = "release_date_conflict"

    elif observed or earlier_announcements or others:
        status = "possible_transfer"

    elif any(
        "possible special or short" in flag
        for flag in flags
    ):
        status = "short_or_special_review"

    elif any(
        "earlier" in flag.lower()
        for flag in flags
    ):
        status = "older_release_unverified"

    else:
        status = "potential_premiere_unverified"

    return status, flags or [
        "No transfer evidence found in incomplete sources; not verified"
    ]


def main():
    OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    feed = fetch_json(GALAXY_URL)

    providers = (
        feed.get("providers")
        if isinstance(feed, dict)
        else None
    )

    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            "Galaxy Movies returned no usable providers"
        )

    if HISTORY.exists():
        history = json.loads(
            HISTORY.read_text(
                encoding="utf-8"
            )
        )
    else:
        history = {}

    if not isinstance(history, dict):
        raise ValueError(
            "Previous streaming-history file must be a dictionary"
        )

    announcements = history.setdefault(
        ANNOUNCEMENTS_KEY, {}
    )

    if not isinstance(announcements, dict):
        raise ValueError(
            "Previously announced arrival dates are invalid"
        )

    now = datetime.now(timezone.utc)
    today = now.date()
    end = today + timedelta(days=DAYS_AHEAD)

    # Capture observed changes and announced dates.
    # An announcement does not prove an actual premiere.
    for provider, groups in providers.items():

        if (
            provider not in HISTORY_SERVICES
            or not isinstance(groups, dict)
        ):
            continue

        for group in ("added", "left", "leavingSoon"):

            for item in groups.get(group) or []:
                ident = movie_id(item)

                if ident is None:
                    continue

                seen = history.setdefault(
                    str(ident), []
                )

                if provider not in seen:
                    seen.append(provider)

        for item in groups.get("coming") or []:
            ident = movie_id(item)

            when = (
                parse_date(item.get("date"))
                if isinstance(item, dict)
                else None
            )

            if ident is None or when is None:
                continue

            by_service = announcements.setdefault(
                str(ident), {}
            )

            # Store earliest announcement.
            # Do not overwrite older evidence.
            older = parse_date(
                by_service.get(provider)
            )

            if older is None or when < older:
                by_service[provider] = when.isoformat()

    candidates = {}

    for provider, groups in providers.items():

        if (
            provider not in SERVICES
            or not isinstance(groups, dict)
        ):
            continue

        for item in groups.get("coming") or []:
            ident = movie_id(item)

            arrival = (
                parse_date(item.get("date"))
                if isinstance(item, dict)
                else None
            )

            if (
                ident is None
                or arrival is None
                or not today <= arrival <= end
            ):
                continue

            record = candidates.setdefault(
                str(ident),
                {
                    "tmdb_id": ident,
                    "media_type": "movie",
                    "title": str(
                        item.get("title") or "Unknown"
                    ),
                    "streaming_dates": {},
                },
            )

            record["streaming_dates"][
                provider
            ] = arrival.isoformat()

    results = []

    for ident, record in candidates.items():
        arrival = min(
            record["streaming_dates"].values()
        )

        record["earliest_streaming_date"] = arrival

        seen = sorted(
            history.get(ident, [])
        )

        record["observed_previous_services"] = seen

        earlier = {
            service: when
            for service, when in announcements.get(
                ident, {}
            ).items()
            if parse_date(when) and when < arrival
        }

        record[
            "earlier_announced_arrivals"
        ] = earlier

        metadata = get_metadata(
            int(ident)
        )

        if metadata:
            record["metadata"] = {
                field: metadata.get(field)
                for field in (
                    "type",
                    "title",
                    "year",
                    "released",
                    "released_digital",
                    "runtime",
                )
            }

        status, flags = classify(
            record,
            seen,
            metadata,
            earlier,
        )

        record["status"] = status
        record["reasons"] = flags

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
        "generated_at_utc": now.isoformat(),
        "purpose": (
            "Diagnostic audit only; no verified Kodi widget"
        ),
        "source": (
            "Powered by Galaxy Movies - "
            "https://www.galaxymovies.app"
        ),
        "metadata_source": "MDBList",
        "metadata_configured": bool(API_KEY),
        "limitations": (
            "MDBList watch_providers are not proof "
            "of past availability; announced dates "
            "may change; change-feed history begins "
            "when this workflow was installed. "
            "No complete historical subscription "
            "catalog is available, so no candidate "
            "is confirmed as a premiere."
        ),
        "counts": dict(
            Counter(
                record["status"]
                for record in results
            )
        ),
        "candidates": results,
    }

    HISTORY.write_text(
        json.dumps(
            history,
            indent=2,
            sort_keys=True
        ) + "\n",
        encoding="utf-8",
    )

    AUDIT.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False
        ) + "\n",
        encoding="utf-8",
    )

    print(
        f"Audited {len(results)} movies "
        f"from {len(providers)} providers"
    )

    print(
        f"Results by status: {audit['counts']}"
    )

    print(
        "No Kodi widget generated; "
        "first-time streaming is unverified"
    )


if __name__ == "__main__":
    main()
