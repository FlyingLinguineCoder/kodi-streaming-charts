
"""Audit upcoming US subscription-streaming movie arrivals.

Galaxy Movies lists platform arrivals, not verified first-time
streaming premieres. This script produces an audit report only.
"""

import json
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FEED_URL = (
    "https://www.galaxymovies.app/api/"
    "streaming-changes?provider=all"
)

# Services to include in our upcoming-premieres widget
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

# Other subscription services checked for possible transfers
SUBSCRIPTION_SERVICES = TARGET_SERVICES | {
    "starz",
    "amc-plus",
    "mgm-plus",
    "crunchyroll",
    "discovery-plus",
    "shudder",
    "criterion",
}

OUTPUT = Path("charts")
HISTORY_PATH = OUTPUT / "premieres_seen_on_streaming.json"
AUDIT_PATH = OUTPUT / "premieres_audit.json"

LOOKAHEAD_DAYS = 120


def get_feed():
    request = urllib.request.Request(
        FEED_URL,
        headers={
            "User-Agent": "KodiStreamingPremieres/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        payload = json.load(response)

    providers = (
        payload.get("providers")
        if isinstance(payload, dict)
        else None
    )

    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            "Unexpected Galaxy Movies response: "
            "no providers object"
        )

    return providers


def movie_id(item):
    if not isinstance(item, dict):
        return None

    if item.get("tmdbType") != "movie":
        return None

    try:
        number = int(item.get("tmdbId"))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def read_history():
    if not HISTORY_PATH.exists():
        return {}

    data = json.loads(
        HISTORY_PATH.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError("Unexpected history format")

    return data


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)

    providers = get_feed()
    history = read_history()

    today = datetime.now(timezone.utc).date()
    latest = today + timedelta(days=LOOKAHEAD_DAYS)

    # Track observed arrivals and departures.
    # This history begins when the script first runs.
    for provider, data in providers.items():
        if provider not in SUBSCRIPTION_SERVICES:
            continue

        if not isinstance(data, dict):
            continue

        for group in ("added", "left", "leavingSoon"):
            for item in data.get(group) or []:
                ident = movie_id(item)

                if ident is None:
                    continue

                services = history.setdefault(
                    str(ident), []
                )

                if provider not in services:
                    services.append(provider)

    candidates = {}

    # Collect upcoming movies from our eight services
    for provider, data in providers.items():
        if provider not in TARGET_SERVICES:
            continue

        if not isinstance(data, dict):
            continue

        for item in data.get("coming") or []:
            ident = movie_id(item)

            if ident is None:
                continue

            try:
                arrives = date.fromisoformat(
                    str(item.get("date", ""))[:10]
                )
            except ValueError:
                continue

            if not today <= arrives <= latest:
                continue

            key = str(ident)

            record = candidates.setdefault(
                key,
                {
                    "tmdb_id": ident,
                    "title": str(
                        item.get("title") or "Unknown"
                    ),
                    "streaming_dates": {},
                },
            )

            record["streaming_dates"][
                provider
            ] = arrives.isoformat()

    results = []

    for key, record in candidates.items():
        observed = sorted(
            history.get(key, [])
        )

        record["observed_previous_services"] = observed

        if observed:
            record["status"] = (
                "exclude_possible_transfer"
            )
        else:
            record["status"] = (
                "needs_first_premiere_verification"
            )

        record["earliest_streaming_date"] = min(
            record["streaming_dates"].values()
        )

        results.append(record)

    # Earliest streaming dates appear first
    results.sort(
        key=lambda entry: (
            entry["earliest_streaming_date"],
            entry["title"],
        )
    )

    audit = {
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "purpose": (
            "Audit only; not a verified "
            "first-time-streaming Kodi widget"
        ),
        "source": (
            "Powered by Galaxy Movies - "
            "https://www.galaxymovies.app"
        ),
        "disclaimer": (
            "The feed lists upcoming arrivals. "
            "It cannot prove first-ever streaming. "
            "Recorded history starts with this script "
            "and may miss earlier transfers."
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

    possible_transfers = sum(
        record["status"] == "exclude_possible_transfer"
        for record in results
    )

    print(
        f"Audited {len(results)} movie candidates "
        f"across {len(providers)} providers"
    )

    print(
        f"Possible transfers: {possible_transfers}"
    )

    print(
        "Audit complete. No Kodi widget generated yet."
    )


if __name__ == "__main__":
    main()
