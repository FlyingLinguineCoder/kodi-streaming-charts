"""Diagnostic: Disney+ and Hulu official monthly press calendars.

This report identifies possible movie arrivals and explicitly announced
series or season premieres. It does not claim first-ever subscription
availability and does not generate a Kodi widget.
"""

import json
import re
import urllib.request
from calendar import month_name
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

OUTPUT = Path("charts/disney_hulu_diagnostic.json")
DAYS_AHEAD = 90

MONTHS = {month_name[number]: number for number in range(1, 13)}
DATE_RE = re.compile(
    r"^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})$",
    re.I,
)
YEAR_RE = re.compile(r"\((19\d{2}|20\d{2})\)")
SEASON_RE = re.compile(r"\bseason\s+(\d+)\b", re.I)

PODCAST_RE = re.compile(r"\bpodcast\b", re.I)
EPISODE_RE = re.compile(
    r"\b(?:new episodes?|finale episodes?|episode \d+|"
    r"special episode|streaming live|live @)\b",
    re.I,
)
SPECIAL_RE = re.compile(
    r"\b(?:holiday special|annual special|special premiere|"
    r"parade|awards show)\b",
    re.I,
)
PREMIERE_RE = re.compile(r"\b(?:premiere|debut)\b", re.I)
COMPLETE_SEASON_RE = re.compile(
    r"\b(?:complete season|all episodes streaming)\b", re.I
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

    # A release year alone can only nominate a movie; it cannot prove a
    # streaming premiere. Explicit TV season markers take precedence.
    if year_match and season is None:
        return "dated_movie_candidate", int(year_match.group(1)), "medium"

    if SPECIAL_RE.search(title):
        return "special_or_event_review", None, "low"

    if explicit_premiere:
        if season is None or season == 1:
            return "series_premiere_candidate", None, "medium"
        return "new_season_premiere_candidate", None, "medium"

    if season is not None and COMPLETE_SEASON_RE.search(title):
        if season == 1:
            return "series_premiere_candidate", None, "medium"
        return "new_season_premiere_candidate", None, "medium"

    if season is not None:
        return "tv_arrival_manual_review", None, "low"

    return "manual_review", None, "low"


def extract(lines, url, year, today, end_date):
    current_date = None
    results = []
    dated_rows = 0

    for line in lines:
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
            "Season arrivals without an explicit premiere or complete-season "
            "label require manual review."
        ),
        "sources": source_reports,
        "results": all_results,
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
