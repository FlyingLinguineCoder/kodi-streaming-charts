
"""Build a Kodi/TMDb Helper upcoming TV premieres feed.

Includes brand-new series and returning seasons.
Uses TVmaze schedules and MDBList for TMDb ID matching.
Does not modify Movies or existing streaming charts.

TVmaze: https://www.tvmaze.com/api (CC BY-SA)
Schedule entries are not independent proof of US rights.
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

TVMAZE = "https://api.tvmaze.com/schedule/full"
MDBLIST = "https://api.mdblist.com/imdb/show/{}"
OUT = Path("charts/upcoming_shows.json")
ID_CACHE = Path("data/tvmaze_imdb_to_tmdb.json")
DAYS_AHEAD = 120
MAX_PLOT_WORDS = 55

SERVICES = {
    "netflix": ("netflix", "Netflix"),
    "amazon prime video": ("amazon-prime-video", "Prime Video"),
    "prime video": ("amazon-prime-video", "Prime Video"),
    "apple tv+": ("apple-tv", "Apple TV+"),
    "apple tv plus": ("apple-tv", "Apple TV+"),
    "apple tv": ("apple-tv", "Apple TV+"),
    "hbo max": ("max", "HBO Max"),
    "max": ("max", "HBO Max"),
    "disney+": ("disney-plus", "Disney+"),
    "disney plus": ("disney-plus", "Disney+"),
    "hulu": ("hulu", "Hulu"),
    "peacock": ("peacock", "Peacock"),
    "paramount+": ("paramount-plus", "Paramount+"),
    "paramount plus": ("paramount-plus", "Paramount+"),
}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plot_text(value):
    if not isinstance(value, str):
        return ""
    parser = PlainText()
    parser.feed(value)
    words = html.unescape(" ".join(parser.parts)).split()
    return " ".join(words[:MAX_PLOT_WORDS]) + (
        "…" if len(words) > MAX_PLOT_WORDS else ""
    )


def country_code(channel):
    country = channel.get("country") if isinstance(channel, dict) else None
    return country.get("code") if isinstance(country, dict) else None


def service_for(show):
    """Avoid inferring streaming rights from unrelated networks."""
    channel = show.get("webChannel")
    if isinstance(channel, dict):
        code = country_code(channel)
        if code and code != "US":
            return None
        name = str(channel.get("name") or "").strip().casefold()
        return SERVICES.get(name)

    network = show.get("network")
    if isinstance(network, dict) and country_code(network) == "US":
        if str(network.get("name") or "").strip().casefold() == "hbo":
            return "max", "HBO Max"
    return None


def parse_day(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def show_of(episode):
    embedded = episode.get("_embedded") or {}
    return embedded.get("show") or episode.get("show") or {}


def candidates(schedule, today):
    if not isinstance(schedule, list):
        raise ValueError("TVmaze schedule was not a JSON list")

    matches = {}

    for episode in schedule:
        if not isinstance(episode, dict):
            continue

        day = parse_day(episode.get("airdate"))
        if day is None or not today <= day <= today + timedelta(days=DAYS_AHEAD):
            continue

        season = episode.get("season")
        if type(season) is not int or season < 1:
            continue

        # Count only the first episode of a season.
        if episode.get("number") != 1:
            continue

        show = show_of(episode)
        if not isinstance(show, dict) or not show.get("name"):
            continue

        service = service_for(show)
        if not service:
            continue

        externals = show.get("externals") or {}
        imdb = externals.get("imdb") if isinstance(externals, dict) else None

        if not isinstance(imdb, str) or not re.fullmatch(r"tt\d{7,12}", imdb):
            continue

        if season == 1:
            first = parse_day(show.get("premiered"))
            if first is not None and first < day:
                continue

        key = (imdb, season)

        record = {
            "imdb_id": imdb,
            "title": show["name"],
            "season": season,
            "date": day,
            "service": service,
            "show": show,
            "source": episode.get("url") or show.get("url"),
        }

        older = matches.get(key)
        if older is None or day < older["date"]:
            matches[key] = record

    return sorted(
        matches.values(),
        key=lambda x: (x["date"], x["title"].casefold())
    )


def imdb_of(metadata):
    ids = metadata.get("ids") or {}
    if not isinstance(ids, dict):
        ids = {}
    return ids.get("imdb") or metadata.get("imdb_id") or metadata.get("imdbid")


def tmdb_of(metadata, imdb):
    if not isinstance(metadata, dict):
        return None

    if metadata.get("type") not in ("show", "tv"):
        return None

    if imdb_of(metadata) != imdb:
        return None

    ids = metadata.get("ids") or {}
    if not isinstance(ids, dict):
        ids = {}

    value = (
        ids.get("tmdb")
        or ids.get("tmdbid")
        or metadata.get("tmdb_id")
        or metadata.get("tmdbid")
    )

    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def build(schedule, resolve, today):
    found = candidates(schedule, today)
    output = []
    seen = set()

    for row in found:
        metadata = resolve(row["imdb_id"])
        ident = tmdb_of(metadata, row["imdb_id"])

        if ident is None or ident in seen:
            continue

        seen.add(ident)

        slug, name = row["service"]
        season = row["season"]
        day = row["date"]

        classification = (
            "New series" if season == 1
            else f"Season {season}"
        )

        heading = (
            f"{classification} listed for {name} on "
            f"{day.strftime('%B')} {day.day}, {day.year}."
        )

        qualification = (
            "Schedule from TVmaze; U.S. streaming "
            "availability not independently verified."
        )

        synopsis = plot_text(row["show"].get("summary"))
        description = (
            (synopsis + "\n\n" if synopsis else "")
            + heading + " " + qualification
        )

        output.append({
            "id": ident,
            "title": row["title"],
            "mediatype": "show",
            "premiere_date": day.isoformat(),
            "premiere_type": "series" if season == 1 else "season",
            "season": season,
            "services": [slug],
            "description": description,
            "source": row["source"] or "https://www.tvmaze.com/api",
            "verification": "tvmaze_schedule_us_unverified",
            "imdb_id": row["imdb_id"],
            "rank": len(output) + 1,
        })

    return output, len(found)


def get_json(url, retries=3):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KodiUpcomingTVPremieres/1.0",
            "Accept": "application/json",
        },
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                request, timeout=65
            ) as response:
                return json.load(response)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ):
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def main():
    key = os.environ.get("MDBLIST_API_KEY")

    if not key:
        raise RuntimeError(
            "MDBLIST_API_KEY secret required"
        )

    schedule = get_json(TVMAZE)

    known = (
        json.loads(ID_CACHE.read_text(encoding="utf-8"))
        if ID_CACHE.exists()
        else {}
    )

    if not isinstance(known, dict):
        raise ValueError("Invalid cached TV show IDs")

    cache = {}
    failures = 0

    def resolve(imdb):
        nonlocal failures

        if imdb not in cache:
            old_id = known.get(imdb)

            if type(old_id) is int and old_id > 0:
                cache[imdb] = {
                    "type": "show",
                    "ids": {
                        "imdb": imdb,
                        "tmdb": old_id,
                    },
                }
                return cache[imdb]

            url = (
                MDBLIST.format(imdb)
                + "?"
                + urllib.parse.urlencode({"apikey": key})
            )

            try:
                cache[imdb] = get_json(url)
                ident = tmdb_of(cache[imdb], imdb)

                if ident is not None:
                    known[imdb] = ident

            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:
                print(
                    f"Metadata unavailable for {imdb}: "
                    f"{type(exc).__name__}"
                )
                failures += 1
                cache[imdb] = None

            time.sleep(0.55)

        return cache[imdb]

    output, discovered = build(
        schedule, resolve, date.today()
    )

    if discovered and failures > max(3, discovered // 3):
        raise RuntimeError(
            "Too many MDBList errors; preserving previous feed"
        )

    if discovered and not output:
        raise RuntimeError(
            "No validated TMDb show IDs; preserving previous feed"
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)

    temp = OUT.with_suffix(".tmp")
    temp.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(OUT)

    ID_CACHE.parent.mkdir(parents=True, exist_ok=True)

    cache_temp = ID_CACHE.with_suffix(".tmp")
    cache_temp.write_text(
        json.dumps(known, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cache_temp.replace(ID_CACHE)

    print(
        f"Upcoming TV candidates: {discovered}; "
        f"validated and saved: {len(output)}"
    )
    print(
        "TVmaze schedule; U.S. subscription "
        "availability not independently verified"
    )


if __name__ == "__main__":
    main()
