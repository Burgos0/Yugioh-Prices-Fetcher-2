import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.candidate_score import build_candidates
from app.tournament_adoption import build_adoption_features
from scripts.import_meta_watch_lists import import_observations_payload


def _observation(card="Original Card", event_date="2026-09-01", **overrides):
    observation = {
        "event_id": "event-one",
        "event_name": "Event One",
        "event_date": event_date,
        "region": "NA",
        "format": "TCG_ADVANCED",
        "banlist_id": "2026-09",
        "player": "Alice",
        "placement": "1st",
        "archetype": "Deck",
        "source_url": "https://ygoprodeck.com/deck/123",
        "source_type": "tournament",
        "source_provider": "ygoprodeck",
        "source_deck_id": "123",
        "published_at": None,
        "main_deck": [{"name": card, "count": 3}],
        "side_deck": [],
        "extra_deck": [],
    }
    observation.update(overrides)
    return observation


class DeckArchivingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")

    def _import_at(self, timestamp, observation):
        with patch("scripts.import_meta_watch_lists._now_iso", return_value=timestamp):
            return import_observations_payload(
                {"observations": [observation]}, dataset_path=self.dataset_path
            )

    def _read(self):
        return json.loads(Path(self.dataset_path).read_text())

    def test_changed_deck_appends_revision_and_leaves_original_immutable(self):
        original_input = _observation(
            archived_at="2000-01-01T00:00:00Z",
            first_seen_at="2000-01-01T00:00:00Z",
        )
        first = self._import_at("2026-09-10T10:00:00Z", original_input)
        self.assertEqual(first["added"], 1)
        stored_original = self._read()["observations"][0]
        self.assertEqual(stored_original["archived_at"], "2026-09-10T10:00:00Z")

        corrected = _observation(
            card="Corrected Card",
            player="Corrected Player",
            event_name="Corrected Event Name",
        )
        second = self._import_at("2026-09-15T12:00:00Z", corrected)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["revisions_added"], 1)

        dataset = self._read()
        self.assertEqual(dataset["observations"], [stored_original])
        self.assertEqual(len(dataset["revisions"]), 1)
        self.assertEqual(dataset["revisions"][0]["player"], "Corrected Player")
        self.assertEqual(dataset["revisions"][0]["archived_at"], "2026-09-15T12:00:00Z")

        before = Path(self.dataset_path).read_bytes()
        third = self._import_at("2026-09-16T12:00:00Z", corrected)
        self.assertEqual(third["revisions_added"], 0)
        self.assertEqual(third["duplicate_existing_skipped"], 1)
        self.assertEqual(Path(self.dataset_path).read_bytes(), before)

    def test_historical_cutoff_uses_archive_time_and_then_event_date(self):
        self._import_at("2026-09-10T10:00:00Z", _observation())
        self._import_at(
            "2026-09-15T12:00:00Z", _observation(card="Corrected Card")
        )

        before_collection = build_adoption_features(
            self.dataset_path, as_of="2026-09-09"
        )
        self.assertEqual(before_collection["total_decks"], 0)

        before_revision = build_adoption_features(
            self.dataset_path, as_of="2026-09-14"
        )
        self.assertEqual(
            [card["card_name"] for card in before_revision["cards"]],
            ["Original Card"],
        )

        after_revision = build_adoption_features(
            self.dataset_path, as_of="2026-09-15"
        )
        self.assertEqual(
            [card["card_name"] for card in after_revision["cards"]],
            ["Corrected Card"],
        )

        future_event_path = str(Path(self.tmp.name) / "future_event.json")
        with patch(
            "scripts.import_meta_watch_lists._now_iso",
            return_value="2026-09-10T10:00:00Z",
        ):
            import_observations_payload(
                {"observations": [_observation(event_date="2026-09-20")]},
                dataset_path=future_event_path,
            )
        future_report = build_adoption_features(
            future_event_path, as_of="2026-09-15"
        )
        self.assertEqual(future_report["total_decks"], 0)
        self.assertEqual(future_report["future_event_date_excluded"], 1)

    def test_unknown_archive_timestamps_are_excluded_and_reported(self):
        missing = _observation(source_deck_id="missing")
        invalid = _observation(
            source_deck_id="invalid", archived_at="not-a-timestamp"
        )
        Path(self.dataset_path).write_text(
            json.dumps({"observations": [missing, invalid]})
        )
        report = build_adoption_features(self.dataset_path, as_of="2026-09-15")
        self.assertEqual(report["total_decks"], 0)
        self.assertEqual(report["unknown_archive_timestamp_excluded"], 2)

    def test_combined_ranking_does_not_mutate_dataset(self):
        self._import_at("2026-09-10T10:00:00Z", _observation())
        before = Path(self.dataset_path).read_bytes()
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=str(Path(self.tmp.name) / "missing.db"),
            as_of="2026-09-15",
        )
        self.assertEqual(report["adoption_total_decks"], 1)
        self.assertEqual(Path(self.dataset_path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
