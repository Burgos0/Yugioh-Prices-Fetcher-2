import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app import create_app
from app.meta_watch import (
    build_meta_watch_report,
    compute_adoption,
    dedupe_observations,
    filter_tournament,
    validate_observation,
    FORMAT_TCG_ADVANCED,
    FORMAT_OCG,
)
from scripts.import_meta_watch_lists import import_observations


def make_obs(event_id, player, archetype, main, side=None, extra=None, published_at=None,
             first_seen_at="2026-09-01T00:00:00Z", fmt=FORMAT_TCG_ADVANCED, banlist_id="2026-04",
             source_type="tournament", event_date="2026-08-30"):
    return {
        "event_id": event_id, "event_name": "Test Event", "event_date": event_date,
        "region": "NA", "format": fmt, "banlist_id": banlist_id, "player": player,
        "placement": "1st", "archetype": archetype, "source_url": "https://example.com/coverage",
        "source_type": source_type, "published_at": published_at, "first_seen_at": first_seen_at,
        "main_deck": [{"name": n, "count": c} for n, c in main],
        "side_deck": [{"name": n, "count": c} for n, c in (side or [])],
        "extra_deck": [{"name": n, "count": c} for n, c in (extra or [])],
    }


class ValidationTests(unittest.TestCase):
    def test_valid_observation_has_no_errors(self):
        obs = make_obs("E1", "Alice", "Kashtira", [("Ash Blossom & Joyous Spring", 3)])
        self.assertEqual(validate_observation(obs), [])

    def test_missing_required_field_is_rejected(self):
        obs = make_obs("E1", "Alice", "Kashtira", [("Ash Blossom & Joyous Spring", 3)])
        del obs["source_url"]
        errors = validate_observation(obs)
        self.assertTrue(any("source_url" in e for e in errors))

    def test_invalid_format_is_rejected(self):
        obs = make_obs("E1", "Alice", "Kashtira", [("Card", 1)], fmt="LEGACY")
        errors = validate_observation(obs)
        self.assertTrue(any("format" in e for e in errors))

    def test_bad_count_is_rejected(self):
        obs = make_obs("E1", "Alice", "Kashtira", [("Card", 1)])
        obs["main_deck"][0]["count"] = 0
        errors = validate_observation(obs)
        self.assertTrue(any("count" in e for e in errors))

    def test_no_card_entries_is_rejected(self):
        obs = make_obs("E1", "Alice", "Kashtira", [])
        errors = validate_observation(obs)
        self.assertTrue(any("no card entries" in e for e in errors))


class DedupeTests(unittest.TestCase):
    def test_same_player_same_event_deduped(self):
        obs1 = make_obs("E1", "Alice", "Kashtira", [("Card A", 1)])
        obs2 = make_obs("E1", "alice ", "Kashtira", [("Card A", 2)])  # same key, different casing/whitespace
        obs3 = make_obs("E1", "Bob", "Kashtira", [("Card A", 1)])
        deduped, dup_count = dedupe_observations([obs1, obs2, obs3])
        self.assertEqual(len(deduped), 2)
        self.assertEqual(dup_count, 1)
        self.assertEqual(deduped[0]["main_deck"][0]["count"], 1)  # keeps first occurrence

    def test_tournament_filter_excludes_casual(self):
        tournament = make_obs("E1", "Alice", "Kashtira", [("Card A", 1)], source_type="tournament")
        casual = make_obs("E2", "Bob", "Kashtira", [("Card A", 1)], source_type="casual")
        result = filter_tournament([tournament, casual])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_id"], "E1")


class AdoptionMathTests(unittest.TestCase):
    def _lists(self, n, card_present_in, archetype="Kashtira", event_prefix="E", copies=1,
               side=False, extra=False):
        lists = []
        for i in range(n):
            main = [("Filler Card", 1)]
            s, e = [], []
            if i in card_present_in:
                main.append(("Tech Card", copies))
                if side:
                    s.append(("Tech Card", 1))
                if extra:
                    e.append(("Tech Card", 1))
            lists.append(make_obs(f"{event_prefix}{i}", f"P{i}", archetype, main, s, e))
        return lists

    def test_denominators_and_adoption_pct(self):
        recent = self._lists(20, card_present_in=set(range(10)))  # 10/20 = 50%
        prior = self._lists(20, card_present_in=set(range(5)), event_prefix="EP")  # 5/20 = 25%
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        self.assertFalse(result["insufficient_data"])
        tech = next(c for c in result["cards"] if c["card_name"] == "Tech Card")
        self.assertEqual(tech["recent_lists"], 10)
        self.assertEqual(tech["recent_total_lists"], 20)
        self.assertAlmostEqual(tech["recent_adoption_pct"], 50.0)
        self.assertAlmostEqual(tech["prior_adoption_pct"], 25.0)
        self.assertAlmostEqual(tech["pct_point_change"], 25.0)

    def test_avg_copies_among_containing_lists(self):
        recent = self._lists(20, card_present_in={0, 1, 2}, copies=2)
        prior = self._lists(20, card_present_in={0}, event_prefix="EP", copies=2)
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        tech = next(c for c in result["cards"] if c["card_name"] == "Tech Card")
        self.assertAlmostEqual(tech["avg_copies"], 2.0)

    def test_main_side_extra_counts(self):
        recent = self._lists(20, card_present_in={0, 1}, side=True, extra=False)
        prior = self._lists(20, card_present_in={0}, event_prefix="EP")
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        tech = next(c for c in result["cards"] if c["card_name"] == "Tech Card")
        self.assertEqual(tech["main_lists"], 2)
        self.assertEqual(tech["side_lists"], 2)
        self.assertEqual(tech["extra_lists"], 0)

    def test_distinct_events_counted(self):
        recent = self._lists(20, card_present_in={0, 1, 2})
        prior = self._lists(20, card_present_in={0}, event_prefix="EP")
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        tech = next(c for c in result["cards"] if c["card_name"] == "Tech Card")
        self.assertEqual(tech["distinct_events"], 3)

    def test_new_card_kept_separate_no_infinite_growth(self):
        recent = self._lists(20, card_present_in={0, 1})
        prior = self._lists(20, card_present_in=set(), event_prefix="EP")
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        self.assertFalse(any(c["card_name"] == "Tech Card" for c in result["cards"]))
        new_names = [c["card_name"] for c in result["new_cards"]]
        self.assertIn("Tech Card", new_names)

    def test_within_archetype_adoption_distinguishes_growth(self):
        # Archetype "Kashtira" grows overall (more Kashtira lists), but the
        # tech card's SHARE within Kashtira lists stays constant -- distinguishable
        # from a card gaining share within a stable-size archetype.
        recent = []
        for i in range(10):
            recent.append(make_obs(f"E{i}", f"P{i}", "Kashtira", [("Filler", 1), ("Tech Card", 1)]))
        for i in range(10, 20):
            recent.append(make_obs(f"E{i}", f"P{i}", "Other", [("Filler", 1)]))
        prior = []
        for i in range(5):
            prior.append(make_obs(f"EP{i}", f"P{i}", "Kashtira", [("Filler", 1), ("Tech Card", 1)]))
        for i in range(5, 20):
            prior.append(make_obs(f"EP{i}", f"P{i}", "Other", [("Filler", 1)]))
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        tech = next(c for c in result["cards"] if c["card_name"] == "Tech Card")
        # Overall adoption grew (10/20=50% vs 5/20=25%)
        self.assertAlmostEqual(tech["pct_point_change"], 25.0)
        kashtira_breakdown = next(b for b in tech["archetype_breakdown"] if b["archetype"] == "Kashtira")
        # Within Kashtira, adoption stayed at 100% both windows -- no within-archetype growth.
        self.assertAlmostEqual(kashtira_breakdown["recent_adoption_pct"], 100.0)
        self.assertAlmostEqual(kashtira_breakdown["prior_adoption_pct"], 100.0)
        self.assertAlmostEqual(kashtira_breakdown["pct_point_change"], 0.0)

    def test_insufficient_lists_flags_insufficient_data(self):
        recent = self._lists(5, card_present_in={0})
        prior = self._lists(20, card_present_in=set(), event_prefix="EP")
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        self.assertTrue(result["insufficient_data"])
        self.assertEqual(result["cards"], [])

    def test_insufficient_events_flags_insufficient_data(self):
        # 20 lists but all from the same event -- fewer than 3 distinct events.
        recent = [make_obs("E1", f"P{i}", "Kashtira", [("Tech Card", 1)]) for i in range(20)]
        prior = self._lists(20, card_present_in=set(), event_prefix="EP")
        result = compute_adoption(recent, prior, min_lists=20, min_events=3)
        self.assertTrue(result["insufficient_data"])


class FormatSeparationAndCutoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.prices_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.prices_path) as conn:
            conn.execute("CREATE TABLE prices (product_id INTEGER, card_name TEXT, set_name TEXT, "
                         "date TEXT, market_price REAL)")

    def _write_dataset(self, observations):
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": observations}, f)

    def test_formats_never_combined(self):
        obs = []
        for i in range(25):
            obs.append(make_obs(f"T{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                                 fmt=FORMAT_TCG_ADVANCED, published_at="2026-09-01T00:00:00Z"))
        for i in range(25):
            obs.append(make_obs(f"O{i}", f"OP{i}", "Kashtira", [("Tech Card", 1)],
                                 fmt=FORMAT_OCG, published_at="2026-09-01T00:00:00Z"))
        self._write_dataset(obs)
        tcg_report = build_meta_watch_report(self.dataset_path, self.prices_path, target_format=FORMAT_TCG_ADVANCED)
        self.assertEqual(tcg_report["other_format_excluded"], 25)
        ocg_report = build_meta_watch_report(self.dataset_path, self.prices_path, target_format=FORMAT_OCG)
        self.assertEqual(ocg_report["other_format_excluded"], 25)

    def test_publication_time_cutoff_uses_published_at_over_first_seen(self):
        # first_seen_at would put this list far in the past window; published_at
        # (the more authoritative signal) places it in the recent window instead.
        recent_obs = [
            make_obs(f"R{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                      published_at="2026-09-14T00:00:00Z", first_seen_at="2026-01-01T00:00:00Z")
            for i in range(20)
        ]
        recent_events_ok = [
            make_obs(f"RA{i}", f"PA{i}", "Kashtira", [("Filler", 1)], published_at="2026-09-14T00:00:00Z")
            for i in range(2)
        ]
        prior_obs = [
            make_obs(f"P{i}", f"PP{i}", "Kashtira", [("Filler", 1)], published_at="2026-08-20T00:00:00Z")
            for i in range(22)
        ]
        self._write_dataset(recent_obs + recent_events_ok + prior_obs)
        report = build_meta_watch_report(self.dataset_path, self.prices_path,
                                          as_of="2026-09-15T00:00:00Z")
        bl = report["banlists"][0]
        self.assertFalse(bl["insufficient_data"])
        self.assertEqual(bl["recent_total_lists"], 22)

    def test_no_observations_for_format_returns_empty_banlists(self):
        self._write_dataset([make_obs("E1", "Alice", "Kashtira", [("Card", 1)], fmt=FORMAT_OCG)])
        report = build_meta_watch_report(self.dataset_path, self.prices_path, target_format=FORMAT_TCG_ADVANCED)
        self.assertEqual(report["banlists"], [])


class CardIdentityMatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.prices_path) as conn:
            conn.execute("CREATE TABLE prices (product_id INTEGER, card_name TEXT, set_name TEXT, "
                         "date TEXT, market_price REAL)")
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "Ash Blossom & Joyous Spring", "Set A", "2026-09-01", 5.0),
                    (2, "Ash Blossom & Joyous Spring (Starlight Rare)", "Set A", "2026-09-01", 50.0),
                    # A distinct card that merely shares a name PREFIX with the one above --
                    # must never be treated as one of its printings.
                    (3, "Ash Blossomtail", "Set B", "2026-09-01", 1.0),
                    (4, "Sky Striker Ace \u2013 Kagari", "Set C", "2026-09-01", 3.0),
                ],
            )

    def test_shared_prefix_does_not_merge_distinct_cards(self):
        from app.meta_watch import resolve_card_printings
        conn = sqlite3.connect(self.prices_path)
        printings = resolve_card_printings(conn, "Ash Blossom & Joyous Spring")
        product_ids = {p["product_id"] for p in printings}
        self.assertEqual(product_ids, {1, 2})
        self.assertNotIn(3, product_ids)  # "Ash Blossomtail" never merged in

    def test_dash_normalization_is_punctuation_only_not_fuzzy_merge(self):
        from app.meta_watch import build_normalized_name_index, resolve_card_printings_with_fallback
        conn = sqlite3.connect(self.prices_path)
        index = build_normalized_name_index(conn)
        # Source uses a plain hyphen; prices.db tracks it with an en dash -- still resolves.
        printings = resolve_card_printings_with_fallback(conn, "Sky Striker Ace - Kagari", index)
        self.assertEqual({p["product_id"] for p in printings}, {4})
        # An unrelated name sharing only a prefix must still not resolve.
        printings = resolve_card_printings_with_fallback(conn, "Ash Blossom", index)
        self.assertEqual(printings, [])


class PriceContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.prices_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.prices_path) as conn:
            conn.execute("CREATE TABLE prices (product_id INTEGER, card_name TEXT, set_name TEXT, "
                         "date TEXT, market_price REAL)")
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "Tech Card", "Test Set", "2026-09-01", 10.00),
                    (1, "Tech Card", "Test Set", "2026-09-08", 10.30),  # +3% -> flat
                ],
            )

    def _write_dataset(self, observations):
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": observations}, f)

    def test_flat_price_flag_and_freshness(self):
        obs = [make_obs(f"E{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                         published_at="2026-09-08T00:00:00Z") for i in range(20)]
        obs += [make_obs(f"EX{i}", f"PX{i}", "Kashtira", [("Filler", 1)] if i else [("Tech Card", 1)],
                          published_at="2026-08-25T00:00:00Z") for i in range(20)]
        # Make sure enough distinct events across both windows.
        self._write_dataset(obs)
        report = build_meta_watch_report(self.dataset_path, self.prices_path, as_of="2026-09-08T00:00:00Z")
        bl = report["banlists"][0]
        tech = next(c for c in bl["cards"] if c["card_name"] == "Tech Card")
        self.assertTrue(tech["resolved"])
        self.assertTrue(tech["price"]["has_price"])
        self.assertTrue(tech["price"]["verified_7d_history"])
        self.assertTrue(tech["price"]["is_flat"])

    def test_unresolved_card_reported_not_guessed(self):
        obs = [make_obs(f"E{i}", f"P{i}", "Kashtira", [("Completely Unknown Card", 1)],
                         published_at="2026-09-08T00:00:00Z") for i in range(20)]
        obs += [make_obs(f"EX{i}", f"PX{i}", "Kashtira", [("Filler", 1)] if i else [("Completely Unknown Card", 1)],
                          published_at="2026-08-25T00:00:00Z") for i in range(20)]
        self._write_dataset(obs)
        report = build_meta_watch_report(self.dataset_path, self.prices_path, as_of="2026-09-08T00:00:00Z")
        bl = report["banlists"][0]
        unknown = next(c for c in bl["cards"] if c["card_name"] == "Completely Unknown Card")
        self.assertFalse(unknown["resolved"])

    def test_missing_price_history_marked_explicitly(self):
        with sqlite3.connect(self.prices_path) as conn:
            conn.execute("DELETE FROM prices")
        obs = [make_obs(f"E{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                         published_at="2026-09-08T00:00:00Z") for i in range(20)]
        obs += [make_obs(f"EX{i}", f"PX{i}", "Kashtira", [("Filler", 1)] if i else [("Tech Card", 1)],
                          published_at="2026-08-25T00:00:00Z") for i in range(20)]
        self._write_dataset(obs)
        report = build_meta_watch_report(self.dataset_path, self.prices_path, as_of="2026-09-08T00:00:00Z")
        bl = report["banlists"][0]
        tech = next(c for c in bl["cards"] if c["card_name"] == "Tech Card")
        self.assertFalse(tech["resolved"])

    def test_missing_prices_db_file_never_crashes_report_generation(self):
        # A fresh checkout has no data/prices.db (it's gitignored and restored
        # from a snapshot, not committed -- see scripts/snapshot_storage.py).
        # Report generation must degrade gracefully, not raise.
        missing_db_path = str(Path(self.tmp.name) / "does_not_exist.db")
        obs = [make_obs(f"E{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                         published_at="2026-09-08T00:00:00Z") for i in range(20)]
        obs += [make_obs(f"EX{i}", f"PX{i}", "Kashtira", [("Filler", 1)] if i else [("Tech Card", 1)],
                          published_at="2026-08-25T00:00:00Z") for i in range(20)]
        self._write_dataset(obs)
        report = build_meta_watch_report(self.dataset_path, missing_db_path, as_of="2026-09-08T00:00:00Z")
        self.assertFalse(report["price_db_available"])
        bl = report["banlists"][0]
        tech = next(c for c in bl["cards"] if c["card_name"] == "Tech Card")
        self.assertFalse(tech["resolved"])
        self.assertTrue(tech["price_db_unavailable"])

    def test_missing_prices_db_page_renders_without_error(self):
        from app import create_app
        missing_db_path = str(Path(self.tmp.name) / "does_not_exist.db")
        obs = [make_obs(f"E{i}", f"P{i}", "Kashtira", [("Tech Card", 1)],
                         published_at="2026-09-08T00:00:00Z") for i in range(20)]
        obs += [make_obs(f"EX{i}", f"PX{i}", "Kashtira", [("Filler", 1)] if i else [("Tech Card", 1)],
                          published_at="2026-08-25T00:00:00Z") for i in range(20)]
        self._write_dataset(obs)
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with unittest.mock.patch("app.routes.META_WATCH_DATASET_PATH", self.dataset_path), \
             unittest.mock.patch("app.routes.build_meta_watch_report",
                                  lambda dataset_path, _prices_path, **kw: build_meta_watch_report(
                                      dataset_path, missing_db_path, as_of="2026-09-08T00:00:00Z")):
            response = client.get("/meta-watch")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Error loading Meta Watch", response.data)
        self.assertIn(b"Price database unavailable", response.data)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.input_path = str(Path(self.tmp.name) / "input.json")

    def _write_input(self, observations):
        with open(self.input_path, "w") as f:
            json.dump(observations, f)

    def test_import_rejects_invalid_and_dedupes(self):
        valid = make_obs("E1", "Alice", "Kashtira", [("Card A", 1)])
        duplicate = make_obs("E1", "Alice", "Kashtira", [("Card A", 2)])
        invalid = make_obs("E2", "Bob", "Kashtira", [])  # no card entries
        self._write_input([valid, duplicate, invalid])
        result = import_observations(self.input_path, dataset_path=self.dataset_path)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["duplicate_in_batch_skipped"], 1)
        self.assertEqual(len(result["rejected"]), 1)

    def test_reimport_skips_existing_duplicates(self):
        valid = make_obs("E1", "Alice", "Kashtira", [("Card A", 1)])
        self._write_input([valid])
        import_observations(self.input_path, dataset_path=self.dataset_path)
        result = import_observations(self.input_path, dataset_path=self.dataset_path)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["duplicate_existing_skipped"], 1)


class PageRenderingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_empty_dataset_renders_explicit_empty_state(self):
        # An empty dataset (the state this feature ships with before any
        # sourced import) must render the explicit empty state, never
        # fixture/placeholder rows as if they were real tournament evidence.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty_dataset = str(Path(tmp.name) / "meta_watch_lists.json")
        with open(empty_dataset, "w") as f:
            json.dump({"schema_version": 1, "observations": []}, f)
        with unittest.mock.patch("app.routes.META_WATCH_DATASET_PATH", empty_dataset):
            response = self.client.get("/meta-watch")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No tournament decklist observations imported yet", response.data)

    def test_real_imported_sample_renders_on_the_live_route(self):
        # Exercises the actual production dataset (data/meta_watch_lists.json)
        # after the sourced sample import -- confirms real imported events
        # render, not just the empty state.
        response = self.client.get("/meta-watch")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"No tournament decklist observations imported yet", response.data)
        self.assertIn(b"UNKNOWN-2026-08", response.data)

    def test_nav_link_present_on_other_pages(self):
        response = self.client.get("/")
        self.assertIn(b"Meta Watch", response.data)


if __name__ == "__main__":
    unittest.main()
