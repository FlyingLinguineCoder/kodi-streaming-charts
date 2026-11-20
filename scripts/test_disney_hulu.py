
"""Diagnostic: Disney+ and Hulu official movie calendars.

Reads November and December 2026 Disney press calendars.
Does not modify existing scripts or generate Kodi widgets.
All results require review before being called premieres.
"""

import json
import re
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

URLS = [
    "https://press.disneyplus.com/news/"
    "next-on-disney-plus-november-2026",
    "https://press.disneyplus.com/news/"
    "next-on-disney-plus-december-2026",
]

OUTPUT = Path("charts/disney_hulu_diagnostic.json")

MONTHS = {
    "January": 1, "February": 2, "March": 3,
    "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9,
    "October": 10, "November": 11, "December": 12,
}

DATE_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|"
    r"Saturday|Sunday),?\s+"
    r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})$",
    re.I,
)

YEAR_RE = re.compile(r"\((19\d{2}|20\d{2})\)")

EXCLUDE_RE = re.compile(
    r"\b(?:season\s+\d+|new episodes?|finale episodes?|"
    r"podcast|shorts|special episode|streaming live|"
    r"new podcast|two-episode premiere|"
    r"three-episode premiere)\b",
    re.I,
)

class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tag = None
        self.buffer = []
        self.lines = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "p", "li"):
            self.tag = tag
            self.buffer = []

    def handle_data(self, data):
        if self.tag:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == self.tag:
            value = " ".join(
                " ".join(self.buffer).split()
            )
            if value:
                self.lines.append(value)
            self.tag = None
            self.buffer = []


def download(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 KodiCalendarAudit/1.0",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        return response.read().decode(
            "utf-8", errors="replace"
        )


def extract(lines, url, year=2026):
    current_date = None
    results = []
    dated_rows = 0

    for line in lines:
        match = DATE_RE.match(line)

        if match:
            month = next(
                (number for name, number in MONTHS.items()
                 if name.lower() == match.group(1).lower()),
                None,
            )
            try:
                current_date = date(
                    year, month, int(match.group(2))
                ).isoformat()
            except (ValueError, TypeError):
                current_date = None
            continue

        if not current_date:
            continue

        service = None
        title = None

        for prefix, services in (
            ("Disney+ and Hulu:", ["disney-plus", "hulu"]),
            ("Disney+:", ["disney-plus"]),
            ("Hulu:", ["hulu"]),
        ):
            if line.startswith(prefix):
                service = services
                title = line[len(prefix):].strip()
                break

        if not service or not title:
            continue

        dated_rows += 1

        year_match = YEAR_RE.search(title)
        release_year = (
            int(year_match.group(1))
            if year_match else None
        )

        if EXCLUDE_RE.search(title):
            classification = "excluded_tv_or_episode"
        elif release_year is not None:
            classification = "dated_movie_candidate"
        else:
            classification = "manual_review"

        results.append({
            "date": current_date,
            "services": service,
            "title": title,
            "release_year": release_year,
            "classification": classification,
            "source": url,
        })

    return results, dated_rows


def main():
    all_results = []
    source_reports = []

    for url in URLS:
        print("Checking:", url)

        try:
            html = download(url)
            parser = CalendarParser()
            parser.feed(html)

            results, count = extract(
                parser.lines, url
            )

            source_reports.append({
                "url": url,
                "status": "parsed",
                "text_blocks": len(parser.lines),
                "dated_entries": count,
                "movie_candidates": sum(
                    r["classification"]
                    == "dated_movie_candidate"
                    for r in results
                ),
            })

            all_results.extend(results)

        except Exception as error:
            source_reports.append({
                "url": url,
                "status": "error",
                "error": str(error),
            })

    if not all_results:
        print(
            "WARNING: No dated entries extracted. "
            "Inspect the page structure."
        )

    OUTPUT.parent.mkdir(
        parents=True, exist_ok=True
    )

    report = {
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "purpose": "Diagnostic only",
        "limitations": (
            "Calendar entries are not proof of "
            "first-ever subscription premieres. "
            "Entries without release years require "
            "manual review. Parsing may miss entries."
        ),
        "sources": source_reports,
        "results": all_results,
    }

    OUTPUT.write_text(
        json.dumps(
            report, indent=2, ensure_ascii=False
        ) + "\n",
        encoding="utf-8",
    )

    print("\nSOURCE REPORT")
    for source in source_reports:
        print(json.dumps(source, indent=2))

    print("\nCLASSIFICATION COUNTS")
    for status in (
        "dated_movie_candidate",
        "manual_review",
        "excluded_tv_or_episode",
    ):
        print(
            status,
            sum(
                r["classification"] == status
                for r in all_results
            ),
        )

    print("\nSaved:", OUTPUT)
    print("No Kodi widget generated.")


if __name__ == "__main__":
    main()
