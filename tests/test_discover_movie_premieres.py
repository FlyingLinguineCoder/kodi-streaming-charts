import sys
import unittest
from copy import deepcopy
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import patch

from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_upcoming_movies as widget
import discover_movie_premieres as discovery


class AutomaticPremieresTest(unittest.TestCase):
    # Exercise a different year to catch month/year URLs tied to the original audit.
    today = date(2028, 12, 20)
    day = date(2029, 2, 6)
    link = "https://whentostream.com/a-new-film-2029/"
    tmdb = "https://www.themoviedb.org/movie/701"
    name = "A New Film"

    def setUp(self):
        self.pages = {}
        self.profiles = {}
        self.set_sources()
        self.audit = {"candidates": []}
        self.history = {}
        self.report = {}

    def set_sources(self, *, service="Netflix", digital_note=None, day=None):
        day = day or self.day
        year = day.year
        self.month_url = dict(discovery.month_pages(self.today, self.today + timedelta(days=90)))[day.replace(day=1)]
        self.pages[self.month_url] = (
            f'<article><h1>Streaming – {day:%B %Y}</h1><h2 class="wp-block-heading">{day:%B} {day.day}, {year}</h2>'
            f'<div class="wp-block-columns"><figure><a href="{self.link}"><img></a></figure></div></article>'
        )
        self.pages[self.link] = (
            f'<article><h1>{self.name} ({year})</h1><div class="entry-content">'
            '<p><strong>Runtime:</strong> 93 mins<br><strong>Genre:</strong> Drama</p>'
            '<p>Director: Ada Rivera</p><p>PVOD Release Date: January 2, 2029</p>'
            f'<p>SVOD Release Date: {day:%B} {day.day}, {year} ({service})</p></div></article>'
        )
        self.search = discovery.TMDB + "/search/movie?query=A+New+Film"
        self.pages[self.search] = f'<title>The Movie Database</title><a href="/movie/701-film"><h2>{self.name}</h2></a>'
        self.pages[self.tmdb] = (
            f'<meta property="og:title" content="{self.name}"><meta property="og:url" content="{self.tmdb}-film">'
            f'<span class="release_date">({year})</span><span class="runtime">1h 33m</span>'
            '<li class="profile"><p>Ada Rivera</p><p>Writer, Director</p></li>'
        )
        self.releases = self.tmdb + '/releases?language=en-US'
        self.pages[self.releases] = (
            '<table class="releases"><thead><tr><th><h2 id="US">United States</h2></th></tr></thead><tbody>'
            f'<tr><td>{day:%m/%d/%Y}</td><td>PG</td><td>Digital</td><td></td><td>{digital_note or service}</td></tr>'
            '</tbody></table>'
        )

    def read(self, url):
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        return html.fromstring(value) if value else None

    def run_widget(self, previous=(), records=(), today=None, *, audit_ok=True):
        return widget.build_widget(
            list(records), self.audit if audit_ok else None, self.history, self.report, list(previous), today or self.today,
            read_source=lambda url: None, discover=partial(discovery.discover_premieres, read=self.read,
                                                            search_profiles=lambda title: self.profiles.get(title, ())),
        )

    def test_unlisted_film_crosses_new_year_for_every_service(self):
        names = dict(widget.SERVICE_NAMES, **{"apple-tv": "Apple TV"})
        for slug, name in names.items():
            with self.subTest(slug=slug):
                self.set_sources(service=name, digital_note="Apple TV+" if slug == "apple-tv" else name)
                item, = self.run_widget()
                self.assertEqual((item["title"], item["id"], item["mediatype"]), (self.name, 701, "movie"))
                self.assertEqual((item["premiere_date"], item["rank"], item["services"]), ("2029-02-06", 1, [slug]))
                self.assertEqual(item["source"], self.link)
                self.assertEqual(item["corroboration"], self.releases)
                self.assertEqual(self.run_widget([item]), [item])

    def test_date_is_bound_to_film_subscription_service_and_us(self):
        for url, old, bad in (
            (self.link, 'SVOD Release Date:', 'VOD Release Date:'),
            (self.link, 'February 6, 2029 (Netflix)', 'February 2029 (Netflix)'),
            (self.link, 'February 6, 2029 (Netflix)', 'February 6, 2029 (Netflix UK)'),
            (self.link, 'February 6, 2029 (Netflix)', 'February 7, 2029 (Netflix)'),
            (self.link, 'February 6, 2029 (Netflix)', 'February 6, 2029 (Netflix) (expected)'),
            (self.link, 'February 6, 2029 (Netflix)', 'February 6, 2029 (MGM+ via Prime Video)'),
            (self.link, '</div>', '<p>SVOD Release Date: November 1, 2028 (Starz)</p></div>'),
            (self.month_url, 'February 6, 2029', 'February 6, 2028'),
            (self.month_url, 'February 6, 2029', 'Coming in February'),
            (self.month_url, self.link, 'https://different.example/film/'),
            (self.releases, 'id="US"', 'id="GB"'),
            (self.releases, '>Digital<', '>Theatrical<'),
            (self.releases, '>Netflix<', '>Amazon Video Store<'),
            (self.releases, '02/06/2029', '06/02/2029'),
        ):
            with self.subTest(url=url, bad=bad):
                old_page = self.pages[url]
                self.pages[url] = old_page.replace(old, bad)
                self.assertEqual(self.run_widget(), [])
                self.pages[url] = old_page

    def test_movie_identity_and_feature_format_are_required(self):
        for url, old, bad in (
            (self.tmdb, self.tmdb+'-film', discovery.TMDB+'/tv/701'),
            (self.tmdb, 'Ada Rivera', 'A Different Director'),
            (self.tmdb, 'Writer, Director', 'Writer'),
            (self.tmdb, '1h 33m', '19m'),
            (self.tmdb, '(2029)', '(1994)'),
            (self.tmdb, self.name, 'A New Film Returns'),
            (self.link, '93 mins', '45 mins'),
            (self.link, 'Drama', 'Stand-up comedy special'),
            (self.link, 'Drama', 'Documentary short film'),
            (self.search, '/movie/701-film', '/tv/701'),
        ):
            with self.subTest(url=url, bad=bad):
                old_page = self.pages[url]
                self.pages[url] = old_page.replace(old, bad)
                self.assertEqual(self.run_widget(), [])
                self.pages[url] = old_page
        self.pages[self.search] += self.pages[self.search].replace('/701-', '/702-')
        self.pages[self.tmdb.replace('701','702')] = self.pages[self.tmdb].replace('/701-', '/702-')
        self.assertEqual(self.run_widget(), [])

    def test_catalog_additions_and_earlier_subscriptions_are_excluded(self):
        self.history = {"701": ["shudder"]}
        self.assertEqual(self.run_widget(), [])
        self.history = {discovery.ANNOUNCEMENTS_KEY: {"701":{"hulu":"2028-12-01"}}}
        self.assertEqual(self.run_widget(), [])
        self.history = {}
        later = self.pages[self.releases]
        earlier = '<tr><td>11/19/2028</td><td></td><td>Digital</td><td></td><td>Shudder</td></tr>'
        self.pages[self.releases] = later.replace('<tbody>', '<tbody>'+earlier)
        self.assertEqual(self.run_widget(), [])
        self.pages[self.releases] = later.replace('<tbody>', '<tbody>'+earlier.replace('Shudder', 'Apple TV Store'))
        self.assertEqual(len(self.run_widget()), 1)

    def test_corroboration_from_catalog_or_single_us_entitlement(self):
        self.set_sources(service="Hulu", digital_note="Rental")
        self.assertEqual(self.run_widget(), [])
        source = "https://press.disneyplus.com/news/next-on-disney-plus-february-2029"
        row = {"source": source, "title": self.name + " – Film Premiere", "release_year": 2029,
               "services": ["hulu"], "classification": "dated_movie_candidate", "date": self.day.isoformat()}
        self.report = {"generated_at_utc": self.today.isoformat(), "sources": [{"url":source, "status":"parsed"}],
                       "results":[row]}
        item, = self.run_widget()
        self.assertEqual(item["corroboration"], source)
        row["services"] = ["disney-plus","hulu"]
        self.assertEqual(self.run_widget(), [])
        self.report = {}
        candidate = {"tmdb_id":701,"media_type":"movie","title":self.name,"metadata":{"type":"movie","title":self.name},
                     "status":"potential_premiere_unverified","streaming_dates":{"hulu":self.day.isoformat()},
                     "earliest_streaming_date":self.day.isoformat()}
        self.audit = {"candidates":[candidate]}
        item, = self.run_widget()
        self.assertEqual(item["corroboration"], discovery.GALAXY_URL)
        for conflict in ({"status":"possible_transfer"},{"other_watch_providers":["starz"]},
                         {"metadata":{"type":"show"}},{"title":"A Different Film"}):
            self.audit = {"candidates":[candidate | conflict]}
            self.assertEqual(self.run_widget([item]), [])

    def test_retry_outage_retraction_and_expiry(self):
        original, = self.run_widget()
        reviewed = [original]
        self.assertEqual(self.run_widget(reviewed, audit_ok=False), reviewed)
        self.assertEqual(self.run_widget(audit_ok=False), [])
        for url in (self.month_url, self.link, self.search, self.tmdb, self.releases):
            page = self.pages[url]
            self.pages[url] = discovery.SourceUnavailable('test outage')
            with self.subTest(url=url):
                self.assertEqual(self.run_widget(reviewed), reviewed)
                self.assertEqual(self.run_widget(), [])
            self.pages[url] = page
        self.assertEqual(self.run_widget(reviewed,today=self.day), reviewed)
        self.assertEqual(self.run_widget(reviewed,today=self.day+timedelta(days=1)), [])
        self.assertEqual(self.run_widget(reviewed,today=self.day+timedelta(days=1),audit_ok=False), [])
        self.pages[self.month_url] = self.pages[self.month_url].replace(self.link, self.link.replace('new-film','different'))
        self.assertEqual(self.run_widget(reviewed), [])

    def test_audit_outage_preserves_reviewed_movies_with_matching_announcement_history(self):
        when = self.day.isoformat()
        record = {"title": self.name, "format": "feature_film", "tmdb": self.tmdb, "date": when,
                  "services": ["netflix"], "source": "https://studio.example/film"}
        self.history = {discovery.ANNOUNCEMENTS_KEY: {"701": {"netflix": when}}}
        previous = [widget.movie_item(701, self.name, self.day, ["netflix"], record["source"]) | {"rank": 1}]
        self.assertEqual(self.run_widget(previous, [record], audit_ok=False), previous)
        self.assertEqual(self.run_widget(records=[record], audit_ok=False), [])
        self.assertEqual(self.run_widget(previous, [record]), [])
        self.history["701"] = ["hulu"]
        self.assertEqual(self.run_widget(previous, [record], audit_ok=False), [])

    def test_schedule_change_is_applied_only_after_independent_confirmation(self):
        old, = self.run_widget()
        new_day = self.day + timedelta(days=3)
        old_confirmation = self.pages[self.releases]
        self.set_sources(day=new_day)
        verified = self.pages[self.releases]
        self.pages[self.releases] = old_confirmation
        self.assertEqual(self.run_widget([old]), [])
        self.pages[self.releases] = verified
        new, = self.run_widget([old])
        self.assertEqual(new['premiere_date'],new_day.isoformat())

    def test_no_duplicate_or_wrong_id_for_reviewed_films_and_stable_ranks(self):
        row = {"title":self.name,"format":"feature_film","tmdb":self.tmdb,
               "source":"https://studio.example/release","services":["netflix"],"date":self.day.isoformat()}
        candidate = {"tmdb_id":701,"title":self.name,"status":"potential_premiere_unverified",
                     "metadata":{"type":"movie","title":self.name},"streaming_dates":{"netflix":self.day.isoformat()},
                     "earliest_streaming_date":self.day.isoformat(),"media_type":"movie"}
        self.audit = {"candidates":[candidate]}
        only, = self.run_widget(records=[row])
        self.assertEqual(only['source'],row['source'])
        self.assertEqual(self.run_widget(records=[row | {"tmdb":discovery.TMDB+'/movie/999'}]), [])
        earlier = row | {"title":"A Prior Film","tmdb":discovery.TMDB+'/movie/800',"date":"2028-12-24"}
        earlier_candidate = candidate | {"title":"A Prior Film","tmdb_id":800,"metadata":{"type":"movie","title":"A Prior Film"},
                                         "earliest_streaming_date":"2028-12-24","streaming_dates":{"netflix":"2028-12-24"}}
        self.audit = {"candidates":[earlier_candidate]}
        both = self.run_widget(records=[earlier])
        self.assertEqual([(x['id'],x['rank']) for x in both],[(800,1),(701,2)])
        self.assertEqual(self.run_widget(deepcopy(both),records=[earlier]), both)

    def test_festival_year_needs_a_documented_premiere_and_matching_credits(self):
        self.pages[self.link] = self.pages[self.link].replace('(2029)', '(2028)')
        self.assertEqual(self.run_widget(), [])
        festival = ('<table class="releases"><thead><tr><th><h2 id="CA">Canada</h2></th></tr></thead>'
                    '<tbody><tr><td>09/04/2028</td><td></td><td>Premiere</td><td></td><td>Festival</td></tr>'
                    '</tbody></table>')
        self.pages[self.releases] += festival
        self.assertEqual(len(self.run_widget()), 1)
        self.pages[self.tmdb] = self.pages[self.tmdb].replace('Ada Rivera', 'Someone Else')
        self.assertEqual(self.run_widget(), [])

    def test_daily_calendar_cannot_mix_different_movies_days_or_entitlements(self):
        self.set_sources(service="Paramount+", digital_note="PVOD")
        daily = 'https://www.movieinsider.com/movies/february/2029/6'
        self.pages[daily] = (
            '<h1>Movies Coming Out February 6, 2029</h1><div class="panel daily"><h3><a>A New Film</a></h3>'
            '<div class="calendar-scope"><strong>Where to Watch:</strong><a>Paramount+</a></div></div>'
        )
        self.assertEqual(self.run_widget()[0]['corroboration'], daily)
        html = self.pages[daily]
        for bad in (html.replace('February 6', 'February 7'), html.replace('A New Film', 'The Other Film'),
                    html.replace('Where to Watch:', 'Buy or rent:'), html.replace('Paramount+', 'Prime Video Store'),
                    html.replace('<a>Paramount+</a>', '<a>Hulu</a><a>Paramount+</a>'),
                    html.replace('<a>Paramount+', '</div></div><div class="panel daily"><h3><a>Other</a></h3><a>Paramount+')):
            self.pages[daily] = bad
            self.assertEqual(self.run_widget(), [])

    def test_official_disney_and_hulu_film_discovery_without_editorial_listing(self):
        del self.pages[self.month_url]
        for service, label in (('disney-plus', 'Disney+'), ('hulu', 'Hulu')):
            source = discovery.DISNEY_PRESS + 'february-2029'
            entry = {'date':self.day.isoformat(), 'services':[service], 'classification':'dated_movie_candidate',
                     'title':f'{self.name} ({label} Original Movie) – Premiere', 'source':source}
            self.report = {'generated_at_utc':self.today.isoformat(), 'sources':[{'url':source,'status':'parsed'}],
                           'results':[entry]}
            self.audit = {'candidates':[{'tmdb_id':701,'media_type':'movie','title':self.name,
                'earliest_streaming_date':self.day.isoformat(), 'streaming_dates':{service:self.day.isoformat()},
                'metadata':{'type':'movie','title':self.name,'released':self.day.isoformat(),'year':2029,'runtime':93},
                'status':'potential_premiere_unverified'}]}
            self.pages[self.releases] = self.pages[self.releases].replace('Netflix',label).replace('Disney+',label)
            with self.subTest(service=service):
                item, = self.run_widget()
                self.assertEqual((item['verification'], item['source'], item['services']), ('us-original-film',source,[service]))
                entry['services'] = ['hulu','disney-plus']
                self.assertEqual(self.run_widget(), [])
                entry['services'] = [service]
                entry['title'] = self.name+' – Premiere'
                self.assertEqual(self.run_widget([item]), [])
                entry['title'] = self.name+' – Film Premiere'
                self.assertEqual(len(self.run_widget()), 1)
                self.report['generated_at_utc'] = '2028-10-01'
                self.assertEqual(self.run_widget(), [])
                self.assertEqual(self.run_widget([item]), [item])

    def test_corroborated_change_to_an_existing_review_cannot_revive_its_old_date(self):
        record = {'title':self.name,'format':'feature_film','tmdb':self.tmdb,'source':'https://studio.example/film',
                  'date':'2029-02-01','services':['netflix']}
        self.history = {discovery.ANNOUNCEMENTS_KEY:{'701':{'netflix':record['date']}}}
        row = {'tmdb_id':701,'title':self.name,'media_type':'movie','earliest_streaming_date':self.day.isoformat(),
               'streaming_dates':{'netflix':self.day.isoformat()}, 'earlier_announced_arrivals':{'netflix':record['date']},
               'metadata':{'type':'movie','title':self.name,'runtime':93},'status':'possible_transfer'}
        self.audit = {'candidates':[row]}
        updated, = self.run_widget(records=[record])
        self.assertEqual(updated['premiere_date'], self.day.isoformat())
        self.assertEqual(self.run_widget([updated], records=[record], audit_ok=False), [updated])
        row['metadata']['runtime'] = 10
        self.assertEqual(self.run_widget([updated], records=[record]), [])
        row['metadata']['runtime'] = 93
        self.history['701'] = ['hulu']
        self.assertEqual(self.run_widget([updated], records=[record]), [])

    def test_film_can_be_announced_before_its_monthly_edition_exists(self):
        del self.pages[self.month_url]
        self.profiles[self.name] = {self.link}
        row = {"tmdb_id":701, "title":self.name, "media_type":"movie", "status":"potential_premiere_unverified",
               "earliest_streaming_date":self.day.isoformat(), "streaming_dates":{"netflix":self.day.isoformat()},
               "metadata":{"type":"movie","title":self.name,"runtime":93}}
        self.audit = {"candidates":[row]}
        item, = self.run_widget()
        self.assertEqual(item["premiere_date"], self.day.isoformat())
        self.assertEqual(self.run_widget([item], today=date(2029, 2, 1)), [item])
        self.pages[self.link] = self.pages[self.link].replace('SVOD Release Date:', 'VOD Release Date:')
        self.assertEqual(self.run_widget([item]), [])
        self.pages[self.link] = None
        self.assertEqual(self.run_widget(), [])

    def test_profile_search_rejects_ambiguous_headlines_and_external_links(self):
        import json
        title = "Pride & Glory"
        results = [
            {"title":"Pride &amp; Glory (2029)","url":self.link},
            {"title":"Pride &amp; Glory (2029)","url":"https://example.org/film/"},
            {"title":"Pride &amp; Glory Sets a Streaming Date","url":self.link+"news/"},
            {"title":"Pride &amp; Glory: The Show (2029)","url":"https://whentostream.com/the-show/"},
        ]
        discovery.find_profiles.cache_clear()
        with patch.object(discovery, "download", return_value=json.dumps(results).encode()):
            self.assertEqual(discovery.find_profiles(title), {self.link})
        discovery.find_profiles.cache_clear()

    def test_stale_or_conflicting_disney_calendar_cannot_corroborate_a_new_film(self):
        self.set_sources(service="Hulu", digital_note="Rental")
        source = discovery.DISNEY_PRESS + 'february-2029'
        entry = {"title":self.name+" – Premiere", "classification":"premiere_type_review",
                 "source":source, "services":["hulu"], "date":self.day.isoformat()}
        self.report = {"generated_at_utc":"2027-01-01", "sources":[{"url":source,"status":"parsed"}], "results":[entry]}
        self.assertEqual(self.run_widget(), [])
        self.report["generated_at_utc"] = self.today.isoformat()
        item, = self.run_widget()
        self.report['results'].append(entry | {"date":"2029-02-15"})
        self.assertEqual(self.run_widget([item]), [])


if __name__ == "__main__":
    unittest.main()
