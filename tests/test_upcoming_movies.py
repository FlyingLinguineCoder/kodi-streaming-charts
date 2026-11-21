import sys
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_upcoming_movies as widget


class UpcomingMoviesTest(unittest.TestCase):
    today = date(2026, 11, 20)

    def setUp(self):
        self.movie = {
            "title": "A Feature", "tmdb": "https://www.themoviedb.org/movie/1001",
            "format": "feature_film", "services": ["netflix"], "date": "2026-11-25",
            "source": "https://studio.example/release",
        }
        self.row = {
            "title": "A Feature", "tmdb_id": 1001, "status": "potential_premiere_unverified",
            "earliest_streaming_date": "2026-11-25", "streaming_dates": {"netflix": "2026-11-25"},
            "metadata": {"year": 2026, "type": "movie"},
        }
        self.audit = {"candidates": [self.row]}

    def build(self, **changes):
        args = dict(records=[self.movie], audit=self.audit, history={}, calendar={},
                    previous=[], today=self.today, read_source=Mock(side_effect=AssertionError))
        return widget.build_widget(**(args | changes))

    def press_movie(self):
        return self.movie | {
            "date_check": "{title} streams on Netflix {date}",
            "corroboration": "https://news.example/film",
            "alternate_date_check": "{date} is the Netflix premiere of {title}",
        }

    def test_all_services_share_one_film_and_one_kodi_movie_id(self):
        services = list(widget.SERVICE_NAMES)
        self.row["streaming_dates"] = dict.fromkeys(services, self.movie["date"])
        record = self.movie | {"format": "feature_documentary", "services": services}
        item, = self.build(records=[record])
        self.assertEqual((item["id"], item["mediatype"], item["rank"]), (1001, "movie", 1))
        self.assertEqual(item["services"], services)
        self.assertEqual(len(services), 8)

    def test_shows_seasons_episodes_and_specials_cannot_enter_the_feed(self):
        formats = ("series", "new_season", "episode", "tv_special", "stand_up", "short", "docuseries")
        other_records = [self.movie | {"format": fmt} for fmt in formats]
        for path in ("tv/1001", "tv/1001/season/2", "tv/1001/season/2/episode/1"):
            other_records.append(self.movie | {"tmdb": "https://www.themoviedb.org/" + path})
        for ident in widget.KNOWN_SPECIALS:
            other_records.append(self.movie | {"tmdb": f"https://www.themoviedb.org/movie/{ident}"})
            self.audit["candidates"].append(self.row | {"tmdb_id": ident})
        self.assertEqual([i["id"] for i in self.build(records=other_records + [self.movie])], [1001])

    def test_catalog_conflicts_block_even_previously_published_movies(self):
        previous = self.build()
        for changed in (
            {"status": "possible_transfer"}, {"media_type": "tv"}, {"tmdbType": "episode"},
            {"metadata": {"type": "show"}}, {"metadata": {"type": "movie", "title": "A Remake"}},
            {"title": "A Series"}, {"earliest_streaming_date": "2026-11-24"},
            {"streaming_dates": {"hulu": "2026-11-25"}}, {"other_watch_providers": ["starz"]},
        ):
            with self.subTest(changed=changed):
                self.assertEqual(self.build(audit={"candidates": [self.row | changed]}, previous=previous), [])
        self.assertEqual(self.build(history={"1001": ["starz"]}, previous=previous), [])
        self.assertEqual(self.build(audit={"candidates": []}, previous=previous), [])
        earlier = {widget.ANNOUNCEMENTS_KEY: {"1001": {"hulu": "2026-11-24"}}}
        self.assertEqual(self.build(history=earlier), [])

    def test_two_dates_or_two_titles_are_not_interchangeable(self):
        movie = self.press_movie()
        def from_press(paragraphs):
            parser = widget.ReleaseText()
            parser.feed(paragraphs)
            parser.close()
            parser.flush()
            return self.build(records=[movie], audit={"candidates": []}, read_source=lambda url: parser.lines)

        self.assertTrue(from_press("<p>A Feature streams on Netflix Nov. 25th, 2026.</p>"))
        for body in (
            "<p>A Feature streams on Netflix November 2.</p>",
            "<p>A Feature streams on Netflix November 25, 2027.</p>",
            "<p>A Feature streams on Netflix in November.</p>",
            "<p>A Feature streams on Netflix</p><p>November 25 is for Another Film.</p>",
            "<script>A Feature streams on Netflix November 25</script>",
        ):
            with self.subTest(body=body):
                self.assertEqual(from_press(body), [])

    def test_alternate_source_is_only_used_on_an_outage(self):
        movie = self.press_movie()
        alternate = [widget.words("November 25 is the Netflix premiere of A Feature")]
        for primary, count in ((None, 1), ([], 0), (["announced for next year"], 0)):
            with self.subTest(primary=primary):
                read = Mock(side_effect=[primary, alternate])
                result = self.build(records=[movie], audit={"candidates": []}, read_source=read)
                self.assertEqual(len(result), count)
                self.assertEqual(read.call_count, 2 if primary is None else 1)
        previous = self.build()
        withdrawal = {widget.ANNOUNCEMENTS_KEY: {"1001": {"netflix": movie["date"]}}}
        unavailable = dict(records=[movie], audit={"candidates": []}, read_source=lambda url: None)
        self.assertEqual(self.build(**unavailable), [])
        self.assertEqual(self.build(**unavailable, previous=previous), previous)
        self.assertEqual(self.build(**unavailable, previous=previous, history=withdrawal), [])

    def test_calendar_withdrawal_blocks_promotion_despite_press_or_old_widget(self):
        source = "https://example.org/calendar-november-2026"
        movie = self.movie | {"calendar_title": "A Feature (2026)", "services": ["hulu"]}
        self.row["streaming_dates"] = {"hulu": movie["date"]}
        calendar = {"window": {"start": "2026-11-20", "end": "2026-12-31"},
                    "sources": [{"url": source, "status": "parsed"}],
                    "results": [{"title": movie["calendar_title"], "date": movie["date"],
                                 "source": source, "services": ["hulu"], "classification": "dated_movie_candidate"}]}
        prev = self.build(records=[movie], calendar=calendar)
        self.assertEqual(len(prev), 1)
        self.assertEqual(self.build(records=[movie], calendar={}, previous=[]), [])
        self.assertEqual(self.build(records=[movie], calendar={}, previous=prev), prev)
        moved = deepcopy(calendar)
        moved["results"][0]["date"] = "2026-11-26"
        self.assertEqual(self.build(records=[movie], calendar=moved, previous=prev), [])

    def test_failed_audit_prunes_expired_or_unreviewed_and_never_adds(self):
        previous = self.build()
        self.assertEqual(self.build(audit=None), [])
        self.assertEqual(self.build(audit=None, previous=previous, today=date(2026, 11, 25)), previous)
        self.assertEqual(self.build(audit=None, previous=previous, today=date(2026, 11, 26)), [])
        self.assertEqual(self.build(records=[], audit=None, previous=previous), [])
        self.assertEqual(self.build(audit=None, previous=[previous[0] | {"mediatype": "show"}]), [])


if __name__ == "__main__":
    unittest.main()
