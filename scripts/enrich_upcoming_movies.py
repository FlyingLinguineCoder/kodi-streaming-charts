
"""Add movie synopses ONLY to charts/upcoming_movies.json.

Run after build_upcoming_movies.py.
Does not modify streaming charts or TV widgets.
"""

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

FEED = Path("charts/upcoming_movies.json")
API = "https://api.mdblist.com/tmdb/movie/{}"
MAX_WORDS = 55


def clean_plot(value):
    if not isinstance(value, str):
        return ""

    value = re.sub(r"<[^>]*>", " ", value)
    value = " ".join(value.split()).strip()

    if not value or value.lower() in {
        "n/a",
        "no overview available",
        "no description available",
    }:
        return ""

    words = value.split()
    return " ".join(words[:MAX_WORDS]) + (
        "…" if len(words) > MAX_WORDS else ""
    )


def verified_plot(movie, metadata):
    """Reject metadata belonging to a different movie."""
    if not isinstance(metadata, dict):
        return ""

    if metadata.get("type") != "movie":
        return ""

    ids = metadata.get("ids") or {}
    if not isinstance(ids, dict):
        return ""

    tmdb_id = (
        ids.get("tmdb")
        or ids.get("tmdbid")
        or metadata.get("tmdbid")
        or metadata.get("tmdb_id")
        or metadata.get("id")
    )

    try:
        if int(tmdb_id) != int(movie["id"]):
            return ""
    except (TypeError, ValueError, KeyError):
        return ""

    return clean_plot(
        metadata.get("overview")
        or metadata.get("description")
        or metadata.get("plot")
    )


def enrich(rows, fetch):
    if not isinstance(rows, list):
        raise ValueError("Expected a list of movie premieres")

    result = []

    for movie in rows:
        if (
            not isinstance(movie, dict)
            or movie.get("mediatype") != "movie"
        ):
            raise ValueError(
                "This script accepts movie premieres only"
            )

        item = movie.copy()
        existing = item.get("description", "")

        marker = "U.S. subscription premiere on "

        if marker in existing:
            announcement = marker + existing.split(marker, 1)[1]
        else:
            announcement = existing.split("\n\n")[-1]

        try:
            metadata = fetch(item["id"])
            plot = verified_plot(item, metadata)
        except (OSError, ValueError, TimeoutError) as error:
            print(
                f"Synopsis unavailable for "
                f"{item.get('title')}: "
                f"{type(error).__name__}"
            )
            plot = ""

        if plot:
            item["description"] = (
                f"{plot}\n\n{announcement}"
                if announcement
                else plot
            )

        result.append(item)

    return result


def main():
    key = os.environ.get("MDBLIST_API_KEY")

    if not key:
        print(
            "MDBLIST_API_KEY absent; "
            "leaving premiere descriptions unchanged"
        )
        return

    rows = json.loads(FEED.read_text(encoding="utf-8"))

    def fetch(ident):
        url = API.format(int(ident))
        url += "?" + urllib.parse.urlencode({
            "apikey": key
        })

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "KodiMoviePremieres/1.0",
            },
        )

        with urllib.request.urlopen(
            request, timeout=12
        ) as response:
            return json.load(response)

    updated = enrich(rows, fetch)

    if updated == rows:
        print("No new verified synopses; feed unchanged")
        return

    temporary = FEED.with_suffix(".json.tmp")

    try:
        temporary.write_text(
            json.dumps(
                updated,
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(FEED)
    finally:
        temporary.unlink(missing_ok=True)

    count = sum(
        old.get("description") != new.get("description")
        for old, new in zip(rows, updated)
    )

    print(
        f"Enriched {count} of {len(rows)} "
        "movie premieres"
    )


if __name__ == "__main__":
    main()
