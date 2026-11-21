"""Build the U.S. Upcoming Movies widget from reviewed feature-film announcements."""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from update_premieres import ANNOUNCEMENTS_KEY, AUDIT, HISTORY, KNOWN_SPECIALS, SERVICE_NAMES

VERIFIED = Path("data/verified_us_movie_premieres.json")
CALENDAR = Path("charts/disney_hulu_diagnostic.json")
OUTPUT = Path("charts/upcoming_movies.json")
FEATURES = {"feature_film", "feature_documentary"}
MOVIE_URL = re.compile(r"https://www\.themoviedb\.org/movie/([1-9]\d*)(?:-[^/?#]+)?/?")


def words(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


class ReleaseText(HTMLParser):
    """Keep visible sentences and table rows apart when checking a dated statement."""
    blocks = {"p", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}
    hidden = {"script", "style", "template", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.hidden_depth = 0
        self.in_row = False
        self.parts = []
        self.lines = []

    def flush(self):
        if self.parts:
            self.lines.append(words(" ".join(self.parts)))
            self.parts.clear()

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag == "tr":
            self.flush()
            self.in_row = True
        elif not self.hidden_depth and not self.in_row and tag in self.blocks:
            self.flush()

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and tag == "tr":
            self.flush()
            self.in_row = False
        elif not self.hidden_depth and not self.in_row and tag in self.blocks:
            self.flush()

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


@lru_cache(maxsize=64)
def source_lines(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KodiMoviePremieres/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code in (404, 410):
            return []
        print(f"Release source unavailable ({error.code}): {url}")
        return None
    except (OSError, HTTPException, LookupError, ValueError) as error:
        print(f"Release source unavailable ({type(error).__name__}): {url}")
        return None
    parser = ReleaseText()
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.lines


def dated_statement(lines, title, day, template):
    before, after = template.replace("{title}", title).split("{date}")
    months = {day.strftime("%B").lower(), day.strftime("%b").lower()}
    if day.month == 9:
        months.add("sept")
    month_pattern = "|".join(sorted(months))
    # An explicit conflicting year must not match the yearless form.
    date_pattern = (
        rf"(?:{month_pattern}) {day.day}(?:st|nd|rd|th)?\b"
        rf"(?: {day.year}\b|(?! (?:19|20)\d{{2}}\b))"
    )
    iso = re.escape(words(day.isoformat()))
    pieces = [re.escape(words(before)), f"(?:{date_pattern}|{iso})", re.escape(words(after))]
    pattern = re.compile(r"\b" + " ".join(piece for piece in pieces if piece) + r"\b")
    return any(pattern.search(line) for line in lines)


def confirms_date(movie, day, read_source):
    """Use the second announcement only on an outage, never to override a mismatch."""
    for url, template in (
        (movie["source"], movie["date_check"]),
        (movie["corroboration"], movie.get("alternate_date_check")),
    ):
        if template:
            lines = read_source(url)
            if lines is not None:
                return dated_statement(lines, movie["title"], day, template)
    return None


def calendar_confirms(report, movie, day, service):
    label = movie.get("calendar_title")
    if not label:
        return True
    window = report.get("window")
    sources = report.get("sources")
    results = report.get("results")
    when = day.isoformat()
    if not isinstance(window, dict) or not isinstance(sources, list) or not isinstance(results, list):
        return None
    if not window.get("start", "~") <= when <= window.get("end", ""):
        return None
    suffix = day.strftime("-%B-%Y").lower()
    parsed = {s["url"] for s in sources if s["status"] == "parsed" and s["url"].endswith(suffix)}
    if not parsed:
        return None
    rows = [r for r in results if r["source"] in parsed and r["title"] == label]
    return not any(r["date"] < when for r in rows) and any(
        r["date"] == when and service in r["services"]
        and r["classification"] in {"dated_movie_candidate", "premiere_type_review"}
        for r in rows
    )


def reviewed_films(records):
    if not isinstance(records, list):
        raise ValueError("Invalid reviewed premiere list")
    seen = set()
    for movie in records:
        if movie["format"] not in FEATURES or movie["tmdb"].startswith("https://www.themoviedb.org/tv/"):
            continue
        match = MOVIE_URL.fullmatch(movie["tmdb"])
        if not match:
            raise ValueError(f"Expected a TMDb movie URL for {movie['title']}")
        ident = int(match.group(1))
        services = movie["services"]
        if (
            ident in seen or not movie["title"].strip()
            or not isinstance(services, list) or not services
            or any(s not in SERVICE_NAMES for s in services)
            or not movie["source"].startswith("https://")
        ):
            raise ValueError(f"Invalid or duplicate reviewed movie: {movie['title']}")
        if "date_check" in movie:
            templates = [movie["date_check"]]
            if "alternate_date_check" in movie:
                templates.append(movie["alternate_date_check"])
            if (
                any(t.count("{date}") != 1 or t.count("{title}") != 1 for t in templates)
                or not movie.get("corroboration", "").startswith("https://")
                or movie["corroboration"] == movie["source"]
            ):
                raise ValueError(f"Invalid date check for {movie['title']}")
        seen.add(ident)
        yield ident, date.fromisoformat(movie["date"]), movie


def audit_agrees(row, title, when):
    # Older reports already screened Galaxy and MDBList for the TMDb movie namespace.
    metadata = row.get("metadata") or {}
    dates = row.get("streaming_dates")
    return (
        row.get("status") == "potential_premiere_unverified"
        and isinstance(dates, dict) and bool(dates) and isinstance(metadata, dict) and bool(metadata)
        and row.get("media_type", "movie") == "movie"
        and row.get("tmdbType", "movie") == "movie"
        and metadata.get("type", "movie") == "movie"
        and words(row["title"]) == words(metadata.get("title") or row["title"]) == words(title)
        and row["earliest_streaming_date"] == min(dates.values()) == when
        and not (row.get("observed_previous_services") or row.get("earlier_announced_arrivals")
                 or row.get("other_watch_providers"))
    )


def build_widget(records, audit, history, calendar, previous, today, read_source=source_lines):
    published = {
        (item["id"], item["title"], item["premiere_date"], service)
        for item in previous if isinstance(item, dict) and item.get("mediatype") == "movie"
        and all(k in item for k in ("id", "title", "premiere_date"))
        for service in item.get("services", [])
    }
    by_id = {}
    for row in audit["candidates"] if audit is not None else ():
        ident = row["tmdb_id"]
        if ident in by_id:
            raise ValueError(f"Ambiguous movie ID in premiere audit: {ident}")
        by_id[ident] = row
    items = []
    end = today + timedelta(days=90)
    for ident, day, movie in reviewed_films(records):
        if not today <= day <= end or ident in KNOWN_SPECIALS:
            continue
        when = day.isoformat()
        title = movie["title"]
        announced = history.get(ANNOUNCEMENTS_KEY, {}).get(str(ident), {})
        if history.get(str(ident)) or any(d < when for d in announced.values()):
            continue
        row = by_id.get(ident)
        if row is not None and not audit_agrees(row, title, when):
            continue
        proof = None
        if audit is not None and "date_check" in movie:
            proof = confirms_date(movie, day, read_source)
        services = []
        for service in dict.fromkeys(movie["services"]):
            already_published = (ident, title, when, service) in published
            if audit is None and not already_published:
                continue
            if row is not None and row["streaming_dates"].get(service) != when:
                continue
            if service in announced and (row is None or announced[service] != when):
                continue  # Do not resurrect a withdrawn or moved catalog announcement.
            if audit is not None and row is None and "date_check" not in movie:
                continue
            if "date_check" in movie and (proof is False or (proof is None and not already_published)):
                continue
            cal = calendar_confirms(calendar, movie, day, service)
            if cal is True or (cal is None and already_published):
                services.append(service)
        if services:
            names = " and ".join(SERVICE_NAMES[s] for s in services)
            items.append({
                "id": ident, "title": title, "mediatype": "movie", "premiere_date": when,
                "services": services, "source": movie["source"],
                "description": f"U.S. subscription premiere on {names}, {day:%B} {day.day}, {day.year}.",
            })
    items.sort(key=lambda item: (item["premiere_date"], words(item["title"]), item["id"]))
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
    return items


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_optional(path, default):
    try:
        data = read_json(path)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        print(f"Could not read {path}: {error}")
        return default
    return data if isinstance(data, type(default)) else default


def main():
    prune_only = os.environ.get("MOVIE_AUDIT_OUTCOME") == "failure"
    if prune_only and not OUTPUT.exists():
        print("No prior movie widget to prune.")
        return
    previous = read_optional(OUTPUT, [])
    calendar = read_optional(CALENDAR, {})
    items = build_widget(
        read_json(VERIFIED), None if prune_only else read_json(AUDIT),
        {} if prune_only else read_json(HISTORY), calendar, previous,
        datetime.now(ZoneInfo("America/New_York")).date(),
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUTPUT.parent) as folder:
        pending = Path(folder) / OUTPUT.name
        pending.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pending.replace(OUTPUT)
    print(f"Saved {len(items)} upcoming U.S. movie premieres to {OUTPUT}.")


if __name__ == "__main__":
    main()
