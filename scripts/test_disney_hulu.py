"""Diagnostic: Disney+ and Hulu official monthly press calendars.

This report identifies possible movie arrivals and explicitly announced
series or season premieres. It does not claim first-ever subscription
availability and does not generate a Kodi widget.
"""

import json
import re
import urllib.request
from calendar import month_name
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

OUTPUT = Path("charts/disney_hulu_diagnostic.json")
MOVIE_AUDIT = Path("charts/premieres_audit.json")
DAYS_AHEAD = 90

MONTHS = {month_name[number]: number for number in range(1, 13)}
DATE_RE = re.compile(
    r"^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})$",
    re.I,
)
YEAR_RE = re.compile(r"\((19\d{2}|20\d{2})\)")
SEASON_RE = re.compile(r"\bseason\s+(\d+)\b", re.I)
SEASON_BUNDLE_RE = re.compile(
    r"\bseasons\s+\d+|\bseason\s+\d+\s*(?:[-–—,&/]|and|to|through)"
    r"\s*(?:season\s+)?\d+\b", re.I,
)
SEASON_PART_RE = re.compile(r"\b(?:part|cour)\s+\d+\b|\bmid[- ]?season\b", re.I)

PODCAST_RE = re.compile(r"\bpodcast\b", re.I)
EPISODE_RE = re.compile(
    r"\b(?:new episodes?|finale episodes?|episode \d+|"
    r"special episode|streaming live|live @)\b",
    re.I,
)
SPECIAL_RE = re.compile(
    r"\b(?:holiday special|holiday short|annual special|special premiere|"
    r"parade|awards show)\b",
    re.I,
)
PREMIERE_RE = re.compile(r"\b(?:premiere|debut)\b", re.I)
TYPED_PREMIERE_RE = re.compile(
    r"\b(series|film|movie)\s+(?:premiere|debut)\b", re.I,
)
CALENDAR_LABEL_RE = re.compile(
    r"\s+[-–—]\s+(?:(?:series|film|movie|season \d+)\s+)?(?:premiere|debut)$", re.I,
)


class CalendarParser(HTMLParser):
    """Keep visible text from the calendar's block-level elements."""

    BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.buffer = []
        self.lines = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.stack.append(tag)
            self.buffer.append([])

    def handle_data(self, data):
        if self.stack:
            self.buffer[-1].append(data)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            value = " ".join(" ".join(self.buffer.pop()).split())
            self.stack.pop()
            if value:
                self.lines.append(value)


def calendar_urls(today):
    """Return published monthly-calendar URLs covering the next 90 days."""
    urls = []
    year, month = today.year, today.month
    last_day = today + timedelta(days=DAYS_AHEAD)

    while (year, month) <= (last_day.year, last_day.month):
        urls.append(
            "https://press.disneyplus.com/news/next-on-disney-plus-"
            f"{month_name[month].lower()}-{year}"
        )
        month += 1
        if month == 13:
            month = 1
            year += 1

    return urls


def download(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 KodiCalendarAudit/2.0",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def classify(title):
    """Classify only from explicit calendar text; keep uncertain rows visible."""
    if PODCAST_RE.search(title):
        return "excluded_podcast", None, "low"

    if EPISODE_RE.search(title):
        return "excluded_episode_or_live_event", None, "low"

    year_match = YEAR_RE.search(title)
    season_match = SEASON_RE.search(title)
    season = int(season_match.group(1)) if season_match else None
    explicit_premiere = bool(PREMIERE_RE.search(title))

    if SPECIAL_RE.search(title):
        return "special_or_event_review", None, "low"

    # A season bundle or a later part can debut on a service mid-run.
    if SEASON_BUNDLE_RE.search(title) or (
        season is not None and SEASON_PART_RE.search(title)
    ):
        return "tv_arrival_manual_review", None, "low"

    if season is not None:
        if season == 0 or not explicit_premiere:
            return "tv_arrival_manual_review", None, "low"
        if season == 1:
            return "series_premiere_candidate", None, "medium"
        return "new_season_premiere_candidate", None, "medium"

    typed = TYPED_PREMIERE_RE.search(title)
    if typed and typed.group(1).casefold() == "series":
        return "series_premiere_candidate", None, "medium"
    if year_match or typed:
        release_year = int(year_match.group(1)) if year_match else None
        return "dated_movie_candidate", release_year, "medium"

    # The calendar uses a bare "Premiere" label for both films and TV.
    if explicit_premiere:
        return "premiere_type_review", None, "low"

    return "manual_review", None, "low"


def extract(lines, url, year, today, end_date):
    current_date = None
    results = []
    dated_rows = 0

    for line in lines:
        # Month-only listings follow the last dated heading on these calendars.
        if line.casefold().startswith("coming in "):
            current_date = None
            continue
        match = DATE_RE.match(line)
        if match:
            month = MONTHS[match.group(1).title()]
            try:
                current_date = date(year, month, int(match.group(2)))
            except ValueError:
                current_date = None
            continue

        if current_date is None or not today <= current_date <= end_date:
            continue

        services = None
        title = None
        for prefix, names in (
            ("Disney+ and Hulu:", ["disney-plus", "hulu"]),
            ("Disney+:", ["disney-plus"]),
            ("Hulu:", ["hulu"]),
        ):
            if line.startswith(prefix):
                services = names
                title = line[len(prefix):].strip()
                break

        if not services or not title:
            continue

        dated_rows += 1
        classification, release_year, confidence = classify(title)
        results.append({
            "date": current_date.isoformat(),
            "services": services,
            "title": title,
            "release_year": release_year,
            "classification": classification,
            "confidence": confidence,
            "source": url,
        })

    return results, dated_rows


def title_key(title):
    return " ".join(title.casefold().replace("’", "'").split())


def movie_review(results, today, end_date):
    """Cross-check dated U.S. arrivals against the saved movie audit."""
    try:
        audit = json.loads(MOVIE_AUDIT.read_text(encoding="utf-8"))
        if not isinstance(audit, dict) or not isinstance(audit.get("candidates"), list):
            raise ValueError("Movie audit has no candidate list")
    except (OSError, ValueError) as error:
        return {"audit_status": "unavailable", "error": str(error), "candidates": []}

    calendar_by_title = defaultdict(list)
    for row in results:
        title = CALENDAR_LABEL_RE.sub("", row["title"])
        title = re.sub(r"\s+\((?:19|20)\d{2}\)$", "", title)
        calendar_by_title[title_key(title)].append(row)

    audit_by_arrival = defaultdict(list)
    for movie in audit["candidates"]:
        if not isinstance(movie, dict):
            continue
        title = movie.get("title")
        dates = movie.get("streaming_dates")
        if not isinstance(title, str) or not title.strip() or not isinstance(dates, dict):
            continue
        for service, when in dates.items():
            if service not in ("disney-plus", "hulu"):
                continue
            try:
                day = date.fromisoformat(when)
            except (TypeError, ValueError):
                continue
            if today <= day <= end_date:
                audit_by_arrival[title_key(title), service, day.isoformat()].append(movie)

    candidates = []
    for (key, service, when), movies in audit_by_arrival.items():
        if len(movies) != 1:
            continue  # The title, date and service must identify one movie.
        movie = movies[0]
        tmdb_id = movie.get("tmdb_id")
        if (
            type(tmdb_id) is not int or tmdb_id <= 0
            or movie.get("status") != "potential_premiere_unverified"
            or movie.get("earliest_streaming_date") != when
        ):
            continue

        rows = calendar_by_title.get(key, [])
        matches = [
            row for row in rows if row["date"] == when and service in row["services"]
        ]
        if rows and (not matches or any(row["date"] < when for row in rows)):
            continue
        metadata = movie.get("metadata")
        year = metadata.get("year") if isinstance(metadata, dict) else None
        if any(
            row["classification"] not in ("dated_movie_candidate", "premiere_type_review")
            or (row["release_year"] is not None and str(row["release_year"]) != str(year))
            for row in matches
        ):
            continue

        candidate = {
            "title": movie["title"],
            "tmdb_id": tmdb_id,
            "service": service,
            "date": when,
            "evidence": "calendar_and_movie_audit" if matches else "movie_audit_only",
        }
        if matches:
            candidate["calendar_source"] = matches[0]["source"]
        candidates.append(candidate)

    return {
        "audit_status": "available",
        "audit_generated_at_utc": audit.get("generated_at_utc"),
        "candidates": sorted(
            candidates, key=lambda row: (row["date"], row["title"], row["service"])
        ),
    }


def main():
    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=DAYS_AHEAD)
    all_results = []
    source_reports = []

    for url in calendar_urls(today):
        print("Checking:", url)
        year = int(url.rsplit("-", 1)[1])
        try:
            parser = CalendarParser()
            parser.feed(download(url))
            results, count = extract(
                parser.lines, url, year, today, end_date
            )
            if not parser.lines:
                raise ValueError("No calendar text blocks were parsed")

            source_reports.append({
                "url": url,
                "status": "parsed",
                "text_blocks": len(parser.lines),
                "upcoming_dated_entries": count,
                "movie_candidates": sum(
                    row["classification"] == "dated_movie_candidate"
                    for row in results
                ),
                "series_premiere_candidates": sum(
                    row["classification"] == "series_premiere_candidate"
                    for row in results
                ),
                "new_season_candidates": sum(
                    row["classification"] == "new_season_premiere_candidate"
                    for row in results
                ),
            })
            all_results.extend(results)
        except Exception as error:
            source_reports.append({
                "url": url,
                "status": "error",
                "error": str(error),
            })

    if not any(source["status"] == "parsed" for source in source_reports):
        raise RuntimeError(
            "No Disney+ monthly calendars could be parsed; "
            "keeping the previous diagnostic output"
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Diagnostic only; no Kodi widget generated",
        "window": {
            "start": today.isoformat(),
            "end": end_date.isoformat(),
        },
        "limitations": (
            "Calendar labels nominate candidates, not confirmed premieres. "
            "Release years do not prove first-ever subscription availability. "
            "An untyped premiere, season bundle, or season arrival without "
            "a premiere label requires review. Month-only listings are omitted. "
            "Movie review records identify films to check for a first U.S. "
            "subscription release. Audit-only dates lack a matching calendar entry."
        ),
        "sources": source_reports,
        "results": all_results,
        "movie_review": movie_review(all_results, today, end_date),
    }
    OUTPUT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("\nSOURCE REPORT")
    for source in source_reports:
        print(json.dumps(source, indent=2))
    print("\nSaved:", OUTPUT)
    print("No Kodi widget generated.")


if __name__ == "__main__":
    main()
