"""Focused offline tests for the rolling candidate-ranker evaluation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.candidate_evaluation import (
    calculate_forward_return,
    evaluate_candidate_ranker,
)


def _observation(
    card_names,
    *,
    event_id="event-1",
    player="Alice",
    archived_at="2026-01-01T12:00:00Z",
):
    return {
        "event_id": event_id,
        "event_name": "Event",
        "event_date": "2026-01-01",
        "region": "NA",
        "format": "TCG_ADVANCED",
        "banlist_id": "2026-01",
        "player": player,
        "placement": "Winner",
        "archetype": "Test",
        "source_url": f"https://example.com/{event_id}/{player}",
        "source_type": "tournament",
        "source_provider": "ygoprodeck",
        "source_deck_id": f"{event_id}-{player}",
        "published_at": None,
        "first_seen_at": archived_at,
        "archived_at": archived_at,
        "main_deck": [{"name": name, "count": 3} for name in card_names],
        "side_deck": [],
        "extra_deck": [],
    }


def _create_prices(path, series):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE prices ("
            "product_id INTEGER, card_name TEXT, set_name TEXT, "
            "low_price REAL, mid_price REAL, high_price REAL, "
            "market_price REAL, direct_low_price REAL, date TEXT, "
            "PRIMARY KEY(product_id, date))"
        )
        rows = []
        for product_id, card_name, start, prices in series:
            start_day = datetime.strptime(start, "%Y-%m-%d")
            rows.extend(
                (
                    product_id,
                    card_name,
                    "Set",
                    (start_day + timedelta(days=index)).strftime("%Y-%m-%d"),
                    price,
                )
                for index, price in enumerate(prices)
            )
        conn.executemany(
            "INSERT INTO prices "
            "(product_id, card_name, set_name, date, market_price) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


class CandidateEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset = Path(self.tmp.name) / "decks.json"
        self.prices = Path(self.tmp.name) / "prices.db"

    def _write_dataset(self, observations, revisions=None):
        self.dataset.write_text(
            json.dumps(
                {
                    "observations": observations,
                    "revisions": revisions or [],
                }
            ),
            encoding="utf-8",
        )

    def test_forward_return_calculation(self):
        self.assertEqual(calculate_forward_return(10.0, 12.5), 25.0)
        self.assertEqual(calculate_forward_return(10.0, 8.0), -20.0)
        self.assertIsNone(calculate_forward_return(0.0, 12.0))
        self.assertIsNone(calculate_forward_return(None, 12.0))
        self.assertIsNone(calculate_forward_return(10.0, None))

    def test_cutoff_uses_archived_version_and_excludes_future_prices(self):
        original = _observation(["Original Card"])
        revision = dict(
            _observation(
                ["Revised Card"], archived_at="2026-01-10T12:00:00Z"
            ),
            source_deck_id=original["source_deck_id"],
        )
        self._write_dataset([original], [revision])
        _create_prices(
            self.prices,
            [
                (1, "Original Card", "2025-12-28", [10.0] * 13 + [12.0]),
                (2, "Revised Card", "2025-12-28", [1.0] * 5 + [500.0] * 8),
            ],
        )

        report = evaluate_candidate_ranker(
            str(self.dataset),
            str(self.prices),
            cutoffs=["2026-01-03"],
            horizons=[7],
            top_k=1,
        )

        cutoff = report["cutoffs"][0]
        self.assertEqual(cutoff["combined_top_product_ids"], [1])
        evaluated = cutoff["horizons"]["7"]["combined_model"][0]
        self.assertEqual(evaluated["cutoff_price_date"], "2026-01-03")
        self.assertEqual(evaluated["target_date"], "2026-01-10")

    def test_empty_and_sparse_data_reports_reasons(self):
        self._write_dataset([])
        missing = evaluate_candidate_ranker(
            str(self.dataset),
            str(self.prices),
            horizons=[7],
        )
        self.assertEqual(missing["eligible_cutoff_count"], 0)
        self.assertEqual(missing["insufficient_data_reasons"]["no_price_data"], 1)

        self._write_dataset([_observation(["Sparse Card"])])
        _create_prices(
            self.prices,
            [(1, "Sparse Card", "2026-01-01", [10.0, 10.0])],
        )
        sparse = evaluate_candidate_ranker(
            str(self.dataset),
            str(self.prices),
            cutoffs=["2026-01-02"],
            horizons=[7],
        )
        self.assertEqual(sparse["ranked_candidate_count"], 1)
        self.assertEqual(sparse["eligible_cutoff_count"], 0)
        self.assertEqual(
            sparse["insufficient_data_reasons"][
                "no_evaluable_combined_return_7d"
            ],
            1,
        )

    def test_combined_model_is_compared_with_price_only_baseline(self):
        observations = [
            _observation(["Adopted Card", "Momentum Card"], player="A"),
            _observation(["Adopted Card"], event_id="event-2", player="B"),
            _observation(["Adopted Card"], event_id="event-3", player="C"),
            _observation(["Adopted Card"], event_id="event-4", player="D"),
        ]
        self._write_dataset(observations)
        _create_prices(
            self.prices,
            [
                (
                    1,
                    "Adopted Card",
                    "2026-01-02",
                    [10.0] * 9 + [None] * 6 + [12.0],
                ),
                (
                    2,
                    "Momentum Card",
                    "2026-01-02",
                    [10.0, 10.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 20.0]
                    + [None] * 6
                    + [15.0],
                ),
            ],
        )

        report = evaluate_candidate_ranker(
            str(self.dataset),
            str(self.prices),
            cutoffs=["2026-01-10"],
            horizons=[7],
            top_k=1,
        )

        cutoff = report["cutoffs"][0]
        self.assertEqual(cutoff["combined_top_product_ids"], [1])
        self.assertEqual(cutoff["price_only_baseline_top_product_ids"], [2])
        summary = report["horizons"]["7"]
        self.assertEqual(summary["combined_model"]["top_k_hit_rate"], 1.0)
        self.assertEqual(
            summary["price_only_baseline"]["top_k_hit_rate"], 0.0
        )
        self.assertGreater(summary["comparison"]["hit_rate_delta"], 0.0)

    def test_output_is_deterministic_and_labeled_retrospective(self):
        self._write_dataset([_observation(["Card"])])
        _create_prices(
            self.prices,
            [(1, "Card", "2026-01-01", [10.0] * 8 + [11.0])],
        )
        kwargs = {
            "cutoffs": ["2026-01-01"],
            "horizons": [7],
            "top_k": 1,
        }
        first = evaluate_candidate_ranker(
            str(self.dataset), str(self.prices), **kwargs
        )
        second = evaluate_candidate_ranker(
            str(self.dataset), str(self.prices), **kwargs
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertEqual(
            first["evaluation_type"], "retrospective_offline_evaluation"
        )
        self.assertIn("does not establish causation", first["notice"])


if __name__ == "__main__":
    unittest.main()
