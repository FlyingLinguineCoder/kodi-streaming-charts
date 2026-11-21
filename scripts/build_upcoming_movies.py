"""Build the U.S. movie widget from reviewed announcements and newly verified releases."""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from calendar import month_name
from datetime import date, datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from discover_movie_premieres import FEATURES, discover_premieres, movie_item, normalized as words
from update_premieres import ANNOUNCEMENTS_KEY, AUDIT, HISTORY, KNOWN_SPECIALS, SERVICE_NAMES, classify

VERIFIED = Path("data/verified_us_movie_premieres.json")
CALENDAR = Path("charts/disney_hulu_diagnostic.json")
OUTPUT = Path("charts/upcoming_movies.json")
MOVIE_URL = re.compile(r"https://www\.themoviedb\.org/movie/([1-9]\d*)(?:-[^/?#]+)?/?")
MONTHS = "|".join(m.lower() for m in month_name[1:])
WEEKDAY = r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday) )?"
DAY_HEADING = re.compile(rf"{WEEKDAY}({MONTHS}) (\d{{1,2}})(?:st|nd|rd|th)?(?: (\d{{4}}))?")
UNCLEAR_HEADING = re.compile(rf"{WEEKDAY}(?:{MONTHS})(?:$| (?:\d|tbd\b|tba\b))|(?:early|mid|late) (?:{MONTHS})\b")


class ReleaseText(HTMLParser):
    """Keep visible sentences and table rows apart when checking a dated statement."""
    blocks = {"p", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}
    hidden = {"script", "style", "template", "noscript", "svg"}

    def __init__(self, split_breaks=False):
        super().__init__()
        self.split_breaks = split_breaks
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
        elif not self.hidden_depth and not self.in_row and (tag in self.blocks or (tag == "br" and self.split_breaks)):
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
def source_lines(url, split_breaks=False):
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
    parser = ReleaseText(split_breaks=split_breaks)
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
    month = rf"(?:{month_pattern})"
    number = rf"{day.day}(?:st|nd|rd|th)?"
    date_pattern = rf"(?:{month} {number}|{number} {month})\b(?: {day.year}\b|(?! (?:19|20)\d{{2}}\b))"
    iso = re.escape(words(day.isoformat()))
    pieces = [re.escape(words(before)), f"(?:{date_pattern}|{iso})", re.escape(words(after))]
    pattern = re.compile(r"\b" + " ".join(piece for piece in pieces if piece) + r"\b")
    return any(pattern.search(line) for line in lines)


def calendar_entry(lines, title, day, rule):
    """Match a complete listing under its own day in a specific year's schedule."""
    heading = words(rule["heading"].replace("{year}", str(day.year)))
    if heading not in lines:
        return False
    start = lines.index(heading) + 1
    if "section" in rule:
        section = words(rule["section"])
        try:
            start = lines.index(section, start) + 1
        except ValueError:
            return False
    entry = words(rule["entry"].replace("{title}", title))
    expected = (day.strftime("%B").lower(), str(day.day), str(day.year))
    current = None
    found = False
    for line in lines[start:]:
        match = DAY_HEADING.fullmatch(line)
        if match:
            month, number, year = match.groups()
            current = (month, str(int(number)), year or str(day.year))
        elif (UNCLEAR_HEADING.match(line) or line in {"coming soon", "date to be announced", "tba", "tbd"}
              or line.startswith(("leaving ", "last chance "))):
            current = None
        elif line == entry and current is not None:
            if current != expected:
                return False
            found = True
    return found


def confirms_date(movie, day, read_source):
    """Use the second announcement only on an outage, never to override a mismatch."""
    for url, template in (
        (movie["source"], movie["date_check"]),
        (movie["corroboration"], movie.get("alternate_date_check")),
    ):
        if template:
            is_calendar = isinstance(template, dict)
            lines = read_source(url, split_breaks=True) if is_calendar else read_source(url)
            if lines is not None:
                if is_calendar:
                    return calendar_entry(lines, movie["title"], day, template)
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


def valid_date_check(rule):
    if isinstance(rule, str):
        return rule.count("{date}") == 1 and rule.count("{title}") == 1
    return (
        isinstance(rule, dict) and {"heading", "entry"} <= rule.keys() <= {"heading", "entry", "section"}
        and isinstance(rule["heading"], str) and "{year}" in rule["heading"]
        and isinstance(rule["entry"], str) and bool(rule["entry"].strip())
        and ("section" not in rule or isinstance(rule["section"], str) and bool(rule["section"].strip()))
    )


def reviewed_films(records):
    if not isinstance(records, list):
        raise ValueError("Invalid reviewed premiere list")
    seen = set()
    for movie in records:
        if movie["tmdb"] is None:
            continue  # The premiere is reviewed but its Kodi movie identity is still unconfirmed.
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
        aliases = movie.get("alternate_titles", [])
        if not isinstance(aliases, list) or any(not isinstance(name, str) or not name.strip() for name in aliases):
            raise ValueError(f"Invalid alternate catalog title for {movie['title']}")
        if "date_check" in movie:
            templates = [movie["date_check"]]
            if "alternate_date_check" in movie:
                templates.append(movie["alternate_date_check"])
            if (
                not all(valid_date_check(t) for t in templates)
                or not movie.get("corroboration", "").startswith("https://")
                or movie["corroboration"] == movie["source"]
            ):
                raise ValueError(f"Invalid date check for {movie['title']}")
        if "superseded_catalog_date" in movie:
            old = date.fromisoformat(movie["superseded_catalog_date"])
            if len(services) != 1 or "date_check" not in movie or old == date.fromisoformat(movie["date"]):
                raise ValueError(f"Invalid catalog date correction for {movie['title']}")
        seen.add(ident)
        yield ident, date.fromisoformat(movie["date"]), movie


def audit_agrees(row, title, when, alternate_titles=()):
    # Older reports already screened Galaxy and MDBList for the TMDb movie namespace.
    metadata = row.get("metadata") or {}
    dates = row.get("streaming_dates")
    titles = {words(name) for name in (title, *alternate_titles)}
    return (
        row.get("status") == "potential_premiere_unverified"
        and isinstance(dates, dict) and bool(dates) and isinstance(metadata, dict) and bool(metadata)
        and row.get("media_type", "movie") == "movie"
        and row.get("tmdbType", "movie") == "movie"
        and metadata.get("type", "movie") == "movie"
        and words(row["title"]) in titles and words(metadata.get("title") or row["title"]) in titles
        and row["earliest_streaming_date"] == min(dates.values()) == when
        and not (row.get("observed_previous_services") or row.get("earlier_announced_arrivals")
                 or row.get("other_watch_providers"))
    )


def apply_reviewed_date(movie, row, announced):
    """Correct just the reviewed service/date pair. Keep the raw audit and history intact."""
    old = movie["superseded_catalog_date"]
    when = movie["date"]
    service, = movie["services"]
    if announced.get(service) == old:
        announced = announced | {service: when}
    dates = (row or {}).get("streaming_dates") or {}
    if dates.get(service) not in {old, when} or row["earliest_streaming_date"] != min(dates.values()):
        return row, announced
    dates = dates | {service: when}
    row = row | {"streaming_dates": dates, "earliest_streaming_date": min(dates.values())}
    earlier = row.get("earlier_announced_arrivals") or {}
    if earlier.get(service) == old:
        row["earlier_announced_arrivals"] = {s: d for s, d in earlier.items() if s != service}
        if row["status"] == "possible_transfer" and not row.get("other_watch_providers"):
            # Recheck the ordinary metadata, runtime, and prior-service rules after removing the false date.
            row["status"], _ = classify(
                row.copy(), row.get("observed_previous_services") or [],
                row.get("metadata"), row["earlier_announced_arrivals"],
            )
    return row, announced


def build_widget(records, audit, history, calendar, previous, today, read_source=source_lines, *, discover=None):
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
        row = by_id.get(ident)
        if "superseded_catalog_date" in movie:
            row, announced = apply_reviewed_date(movie, row, announced)
        if history.get(str(ident)) or any(d < when for d in announced.values()):
            continue
        if row is not None and not audit_agrees(row, title, when, movie.get("alternate_titles", ())):
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
            if service in announced:
                if announced[service] != when:
                    continue
                if audit is not None and row is None and not (day == today and proof is True):
                    continue  # A missing row in a fresh audit can signal a withdrawal.
            if audit is not None and row is None and "date_check" not in movie:
                continue
            if "date_check" in movie and (proof is False or (proof is None and not already_published)):
                continue
            cal = calendar_confirms(calendar, movie, day, service)
            if cal is True or (cal is None and already_published):
                services.append(service)
        if services:
            items.append(movie_item(ident, title, day, services, movie["source"]))
    if discover is not None:
        items.extend(discover(records, audit, history, calendar, previous, today, end,
                              published_ids={item["id"] for item in items}))
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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    prune_only = os.environ.get("MOVIE_AUDIT_OUTCOME") == "failure"
    if prune_only and not OUTPUT.exists():
        print("No prior movie widget to prune.")
        return
    previous = read_optional(OUTPUT, [])
    calendar = read_optional(CALENDAR, {})
    items = build_widget(
        read_json(VERIFIED), None if prune_only else read_json(AUDIT),
        read_optional(HISTORY, {}) if prune_only else read_json(HISTORY), calendar, previous,
        datetime.now(ZoneInfo("America/New_York")).date(),
        discover=discover_premieres,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUTPUT.parent) as folder:
        pending = Path(folder) / OUTPUT.name
        pending.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pending.replace(OUTPUT)
    automatic = sum("verification" in item for item in items)
    print(f"Saved {len(items)} upcoming U.S. movie premieres to {OUTPUT} ({automatic} automatically discovered).")


if __name__ == "__main__":
    main()
