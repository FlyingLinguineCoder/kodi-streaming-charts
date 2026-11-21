"""Find new U.S. subscription film premieres without a per-title configuration."""

import json
import logging
import re
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import unescape
from http.client import HTTPException
from urllib.parse import urlencode

from lxml import etree, html

from test_disney_hulu import CALENDAR_LABEL_RE
from update_premieres import ANNOUNCEMENTS_KEY, GALAXY_URL, KNOWN_SPECIALS, SERVICE_ALIASES, SERVICE_NAMES, classify

logger = logging.getLogger(__name__)

FILM_CALENDAR = "https://whentostream.com"
TMDB = "https://www.themoviedb.org"
PROFILE_URL = re.compile(r"https://whentostream\.com/[a-z0-9][a-z0-9-]*/")
MOVIE_PATH = re.compile(r"/movie/([1-9]\d*)(?:-[^/?#]+)?/?")
# The calendar's SVOD field makes "Apple TV" unambiguous. On TMDb it may mean the store.
SUBSCRIPTIONS = SERVICE_ALIASES | {
    "apple tv plus": "apple-tv", "criterion channel": "criterion", "mubi": "mubi",
    "starz": "starz", "hallmark+": "hallmark-plus", "britbox": "britbox",
}
NONFEATURE = re.compile(
    r"\b(?:stand[ -]up|(?:tv|television|holiday|comedy|concert) special|docu[ -]?series|"
    r"short film|short documentary|limited series|season premiere|live broadcast)\b", re.I
)
US_DATE = r"[A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?, \d{4}"
DISNEY_PRESS = "https://press.disneyplus.com/news/next-on-disney-plus-"
FEATURES = {"feature_film", "feature_documentary"}
FILM_PREMIERE = re.compile(
    r"(?P<title>.+?)(?:\s+\((?P<brand>Disney\+|Hulu) Original(?: (?P<format>Film|Movie))?\))?"
    r"\s+[-–—]\s+(?:World )?(?:(?P<typed>Film|Movie) )?(?:Premiere|Debut)", re.I
)


class SourceUnavailable(Exception):
    """The last verified entry may survive a temporary outage or an unreadable page."""


def normalized(value):
    return " ".join(re.findall(r"\w+", value.casefold()))


def movie_item(ident, title, day, services, source):
    names = " and ".join(SERVICE_NAMES[s] for s in services)
    return {
        "id": ident, "title": title, "mediatype": "movie", "premiere_date": day.isoformat(),
        "services": services, "source": source,
        "description": f"U.S. subscription premiere on {names}, {day:%B} {day.day}, {day.year}.",
    }


def content(node):
    return " ".join(node.text_content().split())


def download(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 KodiMoviePremieres/1.0", "Accept-Language": "en-US",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code in (404, 410):
            return None
        raise SourceUnavailable(f"{url}: HTTP {error.code}") from error
    except (OSError, HTTPException) as error:
        raise SourceUnavailable(f"{url}: {type(error).__name__}") from error


@lru_cache(maxsize=48)
def load_page(url):
    raw = download(url)
    if raw is None:
        return None
    try:
        return html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8"))
    except (ValueError, etree.ParserError) as error:
        raise SourceUnavailable(f"Unreadable page: {url}") from error


@lru_cache(maxsize=64)
def find_profiles(title):
    query = urlencode({"search": title, "type": "post", "subtype": "page", "per_page": 25})
    raw = download(f"{FILM_CALENDAR}/wp-json/wp/v2/search?{query}")
    if raw is None:
        return set()
    try:
        results = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise SourceUnavailable("Unreadable film search") from error
    if not isinstance(results, list):
        raise SourceUnavailable("Unrecognized film search")
    links = set()
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        heading = re.fullmatch(r"(.+) \((?:19|20)\d\d\)", unescape(row["title"]))
        if heading and normalized(heading[1]) == normalized(title) and PROFILE_URL.fullmatch(row.get("url") or ""):
            links.add(row["url"])
    return links


def long_date(value):
    try:
        return datetime.strptime(re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value), "%B %d, %Y").date()
    except ValueError:
        return None


def month_pages(today, end):
    month = today.replace(day=1)
    while month <= end:
        yield month, f"{FILM_CALENDAR}/streaming-{month:%B-%Y}/".lower()
        month = (month + timedelta(days=32)).replace(day=1)


def calendar_links(page, month, today, end):
    """Scope each poster to its full dated heading in this edition of the U.S. calendar."""
    if page is None:
        return {}
    title = page.xpath("string(//article//h1)")
    if normalized(title) != normalized(f"Streaming {month:%B %Y}"):
        raise SourceUnavailable(f"Unrecognized streaming calendar for {month:%B %Y}")
    links = defaultdict(set)
    headings = page.xpath('//article//h2[contains(@class,"wp-block-heading")]')
    if not any(long_date(content(h)) for h in headings):
        raise SourceUnavailable(f"No dated calendar headings for {month:%B %Y}")
    for heading in headings:
        day = long_date(content(heading))
        if not day or day.replace(day=1) != month or not today <= day <= end:
            continue
        for group in heading.itersiblings():
            if group.tag != "div" or "wp-block-columns" not in group.classes:
                break
            for url in group.xpath(".//figure/a[img]/@href"):
                if PROFILE_URL.fullmatch(url):
                    links[url].add(day)
    return links


@dataclass(frozen=True)
class Film:
    title: str
    year: int
    minutes: int
    directors: frozenset
    day: date
    service: str


def film_profile(page):
    if page is None:
        return None
    heading = re.fullmatch(r"(.+) \(((?:19|20)\d\d)\)", page.xpath("string(//article//h1)").strip())
    paragraphs = page.xpath('//article/div[contains(concat(" ",normalize-space(@class)," ")," entry-content ")]/p')
    if not heading or not paragraphs:
        raise SourceUnavailable("Unrecognized film profile")
    # Preserve line breaks between Rating, Runtime, Theater Date and Genre.
    fields = defaultdict(list)
    for para in paragraphs:
        parts = para.xpath(".//text() | .//br")
        text = "".join(part if isinstance(part, str) else "\n" for part in parts)
        for line in text.splitlines():
            label, sep, value = " ".join(line.split()).partition(":")
            if sep:
                fields[label.strip().casefold()].append(value.strip())
    if "svod release date" not in fields and not any("release date" in k for k in fields):
        raise SourceUnavailable("Unrecognized release fields in film profile")
    title, year = heading.groups()
    svod = fields["svod release date"]
    release = re.fullmatch(rf"({US_DATE})\s*\(([^()]+)\)", svod[0], re.I) if len(svod) == 1 else None
    runtime = re.fullmatch(r"(\d+)\s*(?:mins?\.?|minutes?)", " ".join(fields["runtime"]), re.I)
    if not release or not runtime or int(runtime[1]) < 50:
        return None
    day = long_date(release[1])
    provider = release[2].strip().casefold()
    service = "apple-tv" if provider == "apple tv" else SUBSCRIPTIONS.get(provider)
    directors = frozenset(normalized(d) for d in re.split(r",| & | and ", ", ".join(fields["director"])) if d.strip())
    details = " ".join([title, *fields["genre"], *fields["synopsis"]])
    if not day or service not in SERVICE_NAMES or not directors or directors & {"tba", "n a", "unknown"} or NONFEATURE.search(details):
        return None
    return Film(title, int(year), int(runtime[1]), directors, day, service)


def movie_ids(page, title, year):
    if page is None:
        return set()
    ids = set()
    for link in page.xpath('//a[starts-with(@href,"/movie/")][.//h2 or parent::h2]'):
        path = MOVIE_PATH.fullmatch(link.get("href"))
        release = link.xpath('string(following-sibling::span[contains(@class,"release_date")][1])')
        listed_year = re.search(r"\b(?:19|20)\d\d\b", release)
        if listed_year and int(listed_year[0]) not in (year, year + 1):
            continue
        if path and normalized(content(link)) == normalized(title):
            ids.add(int(path[1]))
    if not ids and not page.xpath('//title[contains(., "The Movie Database")]'):
        raise SourceUnavailable("Unrecognized TMDb search page")
    return ids


def us_releases_url(ident):
    return f"{TMDB}/movie/{ident}/releases?language=en-US"


def festival_year(ident, year, read):
    page = read(us_releases_url(ident))
    if page is None:
        return False
    for row in page.xpath('//table[contains(@class,"releases")]/tbody/tr'):
        cells = [content(c) for c in row.xpath("./td")]
        if len(cells) == 5 and cells[2] == "Premiere" and re.fullmatch(rf"\d{{2}}/\d{{2}}/{year}", cells[0]):
            return True
    return False


def same_film(page, ident, film, read):
    if page is None:
        return False
    canonical = page.xpath('string(//meta[@property="og:url"]/@content)')
    title = page.xpath('string(//meta[@property="og:title"]/@content)')
    if not canonical or not title:
        raise SourceUnavailable("Unreadable TMDb movie identity")
    path = MOVIE_PATH.fullmatch(canonical.removeprefix(TMDB))
    year = page.xpath('string(//*[contains(concat(" ",normalize-space(@class)," ")," release_date ")])').strip()
    runtime = page.xpath('string(//*[contains(concat(" ",normalize-space(@class)," ")," runtime ")])').strip()
    duration = re.fullmatch(r"(?:(\d+)h\s*)?(?:(\d+)m)?", runtime)
    minutes = sum(int(v or 0) * scale for v, scale in zip(duration.groups(), (60, 1))) if duration else 0
    directors = set()
    for person in page.xpath('//li[contains(concat(" ",normalize-space(@class)," ")," profile ")]'):
        labels = person.xpath("./p")
        if len(labels) >= 2 and "director" in [j.strip().casefold() for j in content(labels[1]).split(",")]:
            directors.add(normalized(content(labels[0])))
    if not (path and int(path[1]) == ident and normalized(title) == normalized(film.title)
            and minutes >= 50 and abs(minutes - film.minutes) <= 5 and directors & film.directors):
        return False
    return year == f"({film.year})" or (year == f"({film.year + 1})" and festival_year(ident, film.year, read))


def us_subscription_dates(page):
    """Only a named subscription in the U.S. Digital rows can corroborate this window."""
    if page is None:
        return []
    table = page.xpath('//*[@id="US"]/ancestor::table[1]')
    if not table and not page.xpath('//table[contains(@class,"releases")]'):
        raise SourceUnavailable("Unreadable TMDb release tables")
    rows = []
    for row in table[0].xpath("./tbody/tr") if table else ():
        cells = [content(c) for c in row.xpath("./td")]
        if len(cells) != 5 or cells[2] != "Digital":
            continue
        note = cells[4].casefold()
        service = SUBSCRIPTIONS.get(note)
        if not service and any(part.strip() in SUBSCRIPTIONS for part in re.split(r"[,;/|]|\s+(?:and|&)\s+", note)):
            service = "multiple"  # Evidence of a subscription; insufficient to assign an entitlement.
        if service:
            # The request pins the TMDb UI locale to en-US.
            try:
                day = datetime.strptime(cells[0], "%m/%d/%Y").date()
            except ValueError as error:
                raise SourceUnavailable("Unreadable U.S. release date") from error
            rows.append((service, day))
    return rows


def daily_calendar_evidence(film, read):
    url = f"https://www.movieinsider.com/movies/{film.day:%B/%Y}/{film.day.day}".lower()
    page = read(url)
    if page is None:
        return None
    expected = f"Movies Coming Out {film.day:%B} {film.day.day}, {film.day.year}"
    if normalized(page.xpath("string(//h1)")) != normalized(expected):
        raise SourceUnavailable(f"Unrecognized daily film calendar: {url}")
    cards = [card for card in page.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," daily ")]')
             if normalized(card.xpath("string(.//h3/a)")) == normalized(film.title)]
    if len(cards) != 1:
        return None
    for box in cards[0].xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," calendar-scope ")]'):
        if normalized(box.xpath("string(./strong)")) == "where to watch":
            labels = [content(a).casefold() for a in box.xpath("./a")]
            if len(labels) == 1 and SUBSCRIPTIONS.get(labels[0]) == film.service:
                return url
    return None


def current_disney_sources(report, today):
    try:
        generated = datetime.fromisoformat(report.get("generated_at_utc", "")).date()
    except (TypeError, ValueError):
        return set()
    if abs((generated - today).days) > 1:
        return set()
    return {s["url"] for s in report.get("sources", []) if s.get("status") == "parsed"
            and s["url"].startswith(DISNEY_PRESS)}


def disney_evidence(report, parsed, film):
    if film.service not in {"hulu", "disney-plus"}:
        return None
    name = normalized(film.title)
    url = DISNEY_PRESS + f"{film.day:%B-%Y}".lower()
    if url not in parsed:
        return None
    days = set()
    for row in report.get("results", []):
        if (row.get("source") != url or row.get("services") != [film.service]
            or row.get("classification") not in {"dated_movie_candidate", "premiere_type_review"}
            or row.get("release_year") not in (None, film.year)):
            continue
        label = re.sub(r"\s+\((?:19|20)\d\d\)$", "", CALENDAR_LABEL_RE.sub("", row["title"]))
        if normalized(label) == name:
            days.add(row["date"])
    if not days:
        return None
    return url if days == {film.day.isoformat()} else False


def catalog_allows(ident, title, service, day, catalog, history, today, *, audit_available=True):
    """Never turn an observed subscription transfer into a new premiere."""
    when = day.isoformat()
    if ident in KNOWN_SPECIALS or history.get(str(ident)):
        return False
    earlier = history.get(ANNOUNCEMENTS_KEY, {}).get(str(ident), {})
    if any(d < when and (s != service or d < today.isoformat()) for s, d in earlier.items()):
        return False
    row = catalog.get(ident)
    if row is None:
        return not audit_available or service not in earlier or (day == today and earlier[service] == when)
    metadata = row.get("metadata") or {}
    dates = row.get("streaming_dates") or {}
    if (row.get("media_type") != "movie" or row.get("observed_previous_services") or row.get("other_watch_providers")
        or dates.get(service) != when or min(dates.values()) != when or row.get("earliest_streaming_date") != when
        or normalized(row.get("title", "")) != normalized(title)
        or (metadata and (metadata.get("type") != "movie" or normalized(metadata.get("title") or title) != normalized(title)))):
        return False
    prior = row.get("earlier_announced_arrivals") or {}
    if not prior and row.get("status") in {"potential_premiere_unverified", "needs_metadata"}:
        return True
    # A previously announced, still-future date on the same service can be superseded.
    if row.get("status") != "possible_transfer" or set(prior) != {service} or not today.isoformat() <= prior[service] < when:
        return False
    status, _ = classify(row.copy(), [], metadata, {})
    return status == "potential_premiere_unverified"


def official_originals(calendar, parsed, catalog, saved, today, end, reserved, read):
    """Also discover explicitly identified films directly from Disney's U.S. press calendars."""
    old_by_id = {i["id"]: i for i in saved}
    # A missing edition or a failed refresh cannot introduce titles or withdraw its previous entries.
    items = [i for i in saved if i["source"] not in parsed]
    for entry in calendar.get("results", []):
        source = entry.get("source")
        services = entry.get("services")
        if source not in parsed or services not in (["hulu"], ["disney-plus"]):
            continue
        if entry.get("classification") not in {"dated_movie_candidate", "premiere_type_review"}:
            continue
        day = date.fromisoformat(entry["date"])
        match = FILM_PREMIERE.fullmatch(entry["title"])
        if not today <= day <= end or source != DISNEY_PRESS + f"{day:%B-%Y}".lower() or not match:
            continue
        title, brand, original_format, typed = match.groups()
        title = re.sub(r"\s+\((?:19|20)\d\d\)$", "", title)
        service, = services
        if (not (original_format or typed) or NONFEATURE.search(title)
            or (brand and SUBSCRIPTIONS.get(brand.casefold()) != service)):
            continue
        rows = [r for r in catalog.values() if normalized(r.get("title", "")) == normalized(title)
                and r.get("streaming_dates", {}).get(service) == day.isoformat()]
        if len(rows) != 1:
            if not rows and day == today:
                items.extend(i for i in saved if i["source"] == source and i["services"] == services
                             and i["premiere_date"] == day.isoformat() and normalized(i["title"]) == normalized(title))
            continue
        row, = rows
        ident = row["tmdb_id"]
        meta = row.get("metadata") or {}
        # A named film premiere on its first release day, with an independent U.S. subscription listing.
        if (row.get("status") != "potential_premiere_unverified" or meta.get("type") != "movie"
            or normalized(meta.get("title") or "") != normalized(title) or meta.get("released") != day.isoformat()
            or entry.get("release_year") not in (None, meta.get("year"))
            or not isinstance(meta.get("runtime"), (int, float)) or meta["runtime"] < 50
            or reserved(ident, title, service, day)):
            continue
        release_url = us_releases_url(ident)
        try:
            offers = us_subscription_dates(read(release_url))
        except SourceUnavailable:
            old = old_by_id.get(ident)
            if old and old["premiere_date"] == day.isoformat() and old["services"] == services:
                items.append(old)
            continue
        if (service, day) in offers and not any(d < day or (s == service and d != day) for s, d in offers):
            items.append(movie_item(ident, title, day, services, source) | {
                "verification": "us-original-film", "corroboration": release_url,
            })
    return items


def candidate_links(today, end, catalog, published_ids, saved, read, search_profiles):
    """Collect date-scoped film links from monthly editions and advance announcements."""
    dates = defaultdict(set)
    missing_months = set()
    for month, url in month_pages(today, end):
        try:
            page = read(url)
            if page is None:
                raise SourceUnavailable(f"No published U.S. film calendar for {month:%B %Y}")
            for link, days in calendar_links(page, month, today, end).items():
                dates[link].update(days)
        except SourceUnavailable as error:
            missing_months.add(month)
            logger.info("Premiere calendar unavailable: %s", error)
            for link, item in saved.items():
                old = date.fromisoformat(item["premiere_date"])
                if old.replace(day=1) == month:
                    dates[link].add(old)

    # A film can be announced before its monthly calendar exists. Use the live catalog
    # to locate its film profile, then require the same SVOD and identity checks below.
    for ident, row in catalog.items():
        if (ident in published_ids or row.get("status") != "potential_premiere_unverified"
            or row.get("media_type") != "movie" or not row.get("title")):
            continue
        day = date.fromisoformat(row["earliest_streaming_date"])
        if not today <= day <= end or day.replace(day=1) not in missing_months:
            continue
        try:
            for link in search_profiles(row["title"]):
                dates[link].add(day)
        except SourceUnavailable as error:
            logger.info("Film search unavailable for %s: %s", row["title"], error)

    return dates


def discover_premieres(records, audit, history, calendar, previous, today, end, *, published_ids=(),
                       read=load_page, search_profiles=find_profiles):
    """Return verified new rows; retain an existing row only on an outage in its own sources."""
    known_ids = {}
    known_titles = defaultdict(list)
    for movie in records:
        if movie.get("tmdb"):
            path = MOVIE_PATH.fullmatch(movie["tmdb"].removeprefix(TMDB))
            if path:
                known_ids[int(path[1])] = movie
        for title in (movie["title"], *movie.get("alternate_titles", [])):
            known_titles[normalized(title)].append(movie)

    catalog = {r["tmdb_id"]: r for r in (audit or {}).get("candidates", [])}
    parsed = current_disney_sources(calendar, today)

    def reserved(ident, title, service, day):
        if ident in published_ids or not catalog_allows(ident, title, service, day, catalog, history, today,
                                                        audit_available=audit is not None):
            return True
        record = known_ids.get(ident)
        matches = known_titles.get(normalized(title), [])
        if record and record not in matches:
            return True
        for movie in matches:
            if movie["format"] not in FEATURES:
                return True
            if movie.get("tmdb"):
                # Reuse a reviewed identity only for a newly confirmed change to its own service's date.
                if movie is not record or movie["date"] == day.isoformat() or movie["services"] != [service]:
                    return True
                if movie.get("superseded_catalog_date") == day.isoformat():
                    return True
            elif movie["date"] != day.isoformat() or movie["services"] != [service]:
                return True
        return False

    saved = {}
    saved_originals = []
    for item in previous:
        if (not isinstance(item, dict) or item.get("verification") not in {"us-svod", "us-original-film"}
            or item.get("mediatype") != "movie" or len(item.get("services", [])) != 1):
            continue
        day = date.fromisoformat(item["premiere_date"])
        if not today <= day <= end or reserved(item["id"], item["title"], item["services"][0], day):
            continue
        if item["verification"] == "us-svod" and PROFILE_URL.fullmatch(item.get("source", "")):
            saved[item["source"]] = item
        elif item["verification"] == "us-original-film" and item.get("source") == DISNEY_PRESS + f"{day:%B-%Y}".lower():
            saved_originals.append(item)
    if audit is None:
        return [*saved.values(), *saved_originals]

    dates = candidate_links(today, end, catalog, published_ids, saved, read, search_profiles)

    def verify(link):
        old = saved.get(link)
        days = dates[link]
        if old and date.fromisoformat(old["premiere_date"]) not in days:
            old = None
        try:
            film = film_profile(read(link))
            if not film or film.day not in days:
                return None
            matches = known_titles.get(normalized(film.title), [])
            if any(m.get("tmdb") and m["date"] == film.day.isoformat() for m in matches):
                return None
            if old and (old["premiere_date"] != film.day.isoformat() or old["services"] != [film.service]
                        or normalized(old["title"]) != normalized(film.title)):
                old = None
            official = disney_evidence(calendar, parsed, film)
            if official is False:
                return None
            search = TMDB + "/search/movie?" + urlencode({"query": film.title})
            candidates = movie_ids(read(search), film.title, film.year)
            ids = [ident for ident in sorted(candidates) if same_film(read(f"{TMDB}/movie/{ident}"), ident, film, read)]
            if len(ids) != 1:
                return None
            ident, = ids
            if reserved(ident, film.title, film.service, film.day):
                return None
            if old and old["id"] != ident:
                old = None
            release_url = us_releases_url(ident)
            offers = us_subscription_dates(read(release_url))
            if any(d < film.day or (s == film.service and d != film.day) for s, d in offers):
                return None
            corroboration = release_url if (film.service, film.day) in offers else official
            if not corroboration and ident in catalog:
                corroboration = GALAXY_URL
            if not corroboration:
                corroboration = daily_calendar_evidence(film, read)
            if not corroboration:
                return None
            return movie_item(ident, film.title, film.day, [film.service], link) | {
                "verification": "us-svod", "corroboration": corroboration,
            }
        except SourceUnavailable as error:
            logger.info("Premiere verification unavailable for %s: %s", link, error)
            return old

    by_id = defaultdict(list)
    for item in official_originals(calendar, parsed, catalog, saved_originals, today, end, reserved, read):
        by_id[item["id"]].append(item)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for item in pool.map(verify, sorted(dates)):
            if item:
                by_id[item["id"]].append(item)
    return [items[0] for items in by_id.values()
            if len({(normalized(i["title"]), i["premiere_date"], tuple(i["services"])) for i in items}) == 1]
