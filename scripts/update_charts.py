
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

# MDBList API authentication
API_KEY = os.environ["MDBLIST_API_KEY"]

# MDBList JustWatch streaming charts endpoint
BASE_URL = "https://api.mdblist.com/justwatch/streaming-charts"

# US streaming providers
PROVIDERS = {
    "netflix": "nfx",
    "prime": "amp",
    "apple": "atp",
    "max": "mxx",
    "disney": "dnp",
    "hulu": "hlu",
    "paramount": "ppp",
    "peacock": "pct",
}

# Movies and TV shows
MEDIA_TYPES = {
    "movies": "movie",
    "shows": "show",
}


def fetch_chart(media_type, provider):

    params = {
        "apikey": API_KEY,
        "country": "US",
        "period": "daily",
        "provider": provider,
        "genre": "all",
    }

    url = (
        f"{BASE_URL}/{media_type}?"
        + urllib.parse.urlencode(params)
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KodiStreamingCharts/1.0"
        }
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(
            response.read().decode("utf-8")
        )

    # Handle either supported response structure
    if isinstance(payload, list):
        items = payload

    elif isinstance(payload, dict):
        items = payload.get(
            "results",
            payload.get("items", [])
        )

    else:
        raise ValueError(
            "Unexpected MDBList API response."
        )

    if not isinstance(items, list) or not items:
        raise ValueError(
            f"No chart entries returned for "
            f"{media_type}/{provider}"
        )

    output = []

    for position, item in enumerate(items, start=1):

        ids = item.get("ids") or {}

        # Find the TMDb ID
        tmdb_id = (
            item.get("tmdb_id")
            or item.get("tmdbid")
            or ids.get("tmdb")
            or item.get("id")
        )

        if not tmdb_id:
            continue

        # TMDb Helper-compatible entry
        entry = {
            "id": int(tmdb_id),
            "rank": item.get("rank") or position,
            "adult": item.get("adult", 0),
            "title": item.get("title", ""),
            "mediatype": media_type,
        }

        imdb_id = (
            item.get("imdb_id")
            or ids.get("imdb")
        )

        if imdb_id:
            entry["imdb_id"] = imdb_id

        year = (
            item.get("release_year")
            or item.get("year")
        )

        if year:
            entry["release_year"] = year

        output.append(entry)

    if not output:
        raise ValueError(
            f"No usable TMDb IDs found for "
            f"{media_type}/{provider}"
        )

    return output


def main():

    output_dir = Path("charts")
    output_dir.mkdir(exist_ok=True)

    total_charts = 0

    for name, provider in PROVIDERS.items():

        for folder, media_type in MEDIA_TYPES.items():

            filename = f"{name}_{folder}.json"
            filepath = output_dir / filename

            print(f"Fetching {filename}...")

            entries = fetch_chart(
                media_type,
                provider
            )

            with filepath.open(
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    entries,
                    file,
                    ensure_ascii=False,
                    indent=2
                )

            print(
                f"Saved {filename}: "
                f"{len(entries)} titles"
            )

            total_charts += 1

    print(
        f"Successfully updated "
        f"{total_charts} streaming charts."
    )


if __name__ == "__main__":
    main()
