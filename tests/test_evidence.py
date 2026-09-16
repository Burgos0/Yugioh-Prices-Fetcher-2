import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.evidence import (
    EVIDENCE_TYPES,
    MAPPING_UNCERTAINTY_VALUES,
    SCHEMA_VERSION,
    EvidenceValidationError,
    append_records,
    build_record,
    compute_available_at,
    iter_records,
    latest_by_logical_key,
    visible_at,
)


class BuildRecordTests(unittest.TestCase):
    def test_defaults_are_populated_and_available_at_is_derived(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg",
            source_id="post-42",
            event_time="2026-09-10",
            publication_time="2026-09-10T15:00:00Z",
            value=1,
            product_id=94958,
        )
        self.assertEqual(r.schema_version, SCHEMA_VERSION)
        self.assertEqual(r.evidence_type, "announcement")
        self.assertEqual(r.mapping_uncertainty, "exact")
        self.assertEqual(r.product_id, 94958)
        self.assertTrue(r.record_id)
        self.assertTrue(r.first_seen_at.endswith("Z"))
        # available_at = max(publication_time, event_time)
        self.assertEqual(r.available_at, "2026-09-10T15:00:00Z")

    def test_available_at_uses_first_seen_when_publication_unknown(self):
        # publication_time unknown: available_at falls back to first_seen_at
        # (never to event_time), so replays never claim we could have acted
        # before we actually saw the fact.
        r = build_record(
            evidence_type="tournament_adoption",
            source="konami",
            source_id="ycs-montreal-2026-08",
            event_time="2026-08-16",  # tournament date
            publication_time=None,
            first_seen_at="2026-08-20T12:00:00Z",
            value=3,
            product_id=1,
        )
        self.assertEqual(r.available_at, "2026-08-20T12:00:00Z")

    def test_available_at_never_regresses_below_event_time(self):
        # If publication_time exists but is *earlier* than event_time
        # (e.g. a pre-announcement), available_at must still be at least
        # event_time — the fact is not actionable before the event.
        r = build_record(
            evidence_type="announcement",
            source="ygorg",
            source_id="preview-1",
            event_time="2026-09-10T00:00:00Z",
            publication_time="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-01T00:00:00Z",
            value=1,
            product_id=1,
        )
        self.assertEqual(r.available_at, "2026-09-10T00:00:00Z")


class MissingIsNotZeroTests(unittest.TestCase):
    def test_missing_value_requires_reason(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="sales_velocity",
                source="tcgplayer",
                source_id="94958",
                value=None,
                # no missing_reason -> reject
                product_id=94958,
            )

    def test_present_value_rejects_missing_reason(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="sales_velocity",
                source="tcgplayer",
                source_id="94958",
                value=0.0,  # explicit zero is NOT missing
                missing_reason="feed_gap",
                product_id=94958,
            )

    def test_zero_is_a_valid_present_value_distinct_from_missing(self):
        r = build_record(
            evidence_type="sales_velocity",
            source="tcgplayer",
            source_id="94958",
            value=0.0,
            value_unit="sales_per_day",
            product_id=94958,
        )
        self.assertEqual(r.value, 0.0)
        self.assertIsNone(r.missing_reason)

    def test_missing_with_reason_is_accepted(self):
        r = build_record(
            evidence_type="listing_supply",
            source="tcgplayer",
            source_id="94958",
            value=None,
            missing_reason="endpoint_not_yet_integrated",
            product_id=94958,
        )
        self.assertIsNone(r.value)
        self.assertEqual(r.missing_reason, "endpoint_not_yet_integrated")


class MappingUncertaintyTests(unittest.TestCase):
    def test_exact_requires_product_id(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg",
                source_id="post-1",
                value=1,
                mapping_uncertainty="exact",
                product_id=None,
            )

    def test_unresolved_forbids_product_id(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg",
                source_id="post-1",
                value=1,
                mapping_uncertainty="unresolved",
                product_id=99,
            )

    def test_ambiguous_can_list_candidates_in_evidence(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg",
            source_id="post-1",
            value=1,
            mapping_uncertainty="ambiguous",
            product_id=None,
            evidence={"candidates": [111, 222, 333]},
        )
        self.assertEqual(r.evidence["candidates"], [111, 222, 333])

    def test_unknown_vocabulary_values_rejected(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="not-a-real-type",
                source="x", source_id="y", value=1, product_id=1,
            )
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="x", source_id="y", value=1, product_id=1,
                mapping_uncertainty="perhaps",
            )


class AppendAndIterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "evidence.jsonl")

    def _rec(self, **kw):
        base = dict(
            evidence_type="price_momentum",
            source="early_movers",
            source_id="94958-2026-09-11",
            value=0.42,
            product_id=94958,
        )
        base.update(kw)
        return build_record(**base)

    def test_append_creates_parent_directory_and_is_append_only(self):
        n1 = append_records(self.path, [self._rec()])
        n2 = append_records(self.path, [self._rec(source_id="94958-2026-09-12")])
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)
        got = list(iter_records(self.path))
        self.assertEqual(len(got), 2)
        # historical record preserved verbatim
        self.assertEqual(got[0].source_id, "94958-2026-09-11")
        self.assertEqual(got[1].source_id, "94958-2026-09-12")

    def test_iter_records_rejects_malformed_lines(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json\n")
        with self.assertRaises(EvidenceValidationError):
            list(iter_records(self.path))

    def test_iter_records_rejects_lines_missing_required_fields(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"record_id": "x"}) + "\n")
        with self.assertRaises(EvidenceValidationError):
            list(iter_records(self.path))

    def test_iter_records_on_missing_file_yields_nothing(self):
        self.assertEqual(list(iter_records(self.path + ".nope")), [])


class RevisionAndReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "evidence.jsonl")

    def test_supersedes_replaces_latest_but_preserves_history(self):
        original = build_record(
            evidence_type="announcement",
            source="ygorg",
            source_id="post-1",
            publication_time="2026-09-10T00:00:00Z",
            first_seen_at="2026-09-10T00:00:00Z",
            value=1,
            product_id=100,
            evidence={"note": "first read"},
        )
        revised = build_record(
            evidence_type="announcement",
            source="ygorg",
            source_id="post-1",
            publication_time="2026-09-10T00:00:00Z",
            first_seen_at="2026-09-11T00:00:00Z",
            value=1,
            product_id=100,
            evidence={"note": "corrected reading"},
            supersedes=original.record_id,
        )
        append_records(self.path, [original, revised])
        stored = list(iter_records(self.path))
        # Both records are still on disk; nothing silently overwritten.
        self.assertEqual(len(stored), 2)
        latest = latest_by_logical_key(stored)
        current = latest[("ygorg", "post-1")]
        self.assertEqual(current.evidence["note"], "corrected reading")
        # And the original is not itself the tail of any chain.
        self.assertNotIn(original.record_id, {r.record_id for r in latest.values()})

    def test_visible_at_hides_records_not_yet_available(self):
        # Fact only becomes actionable on 2026-09-15; a replay at 2026-09-14
        # must not see it. This is the property that makes historical
        # backtests trustworthy.
        r_early = build_record(
            evidence_type="tournament_adoption",
            source="konami",
            source_id="ycs-2026-09-15",
            event_time="2026-09-15",
            publication_time="2026-09-15T18:00:00Z",
            first_seen_at="2026-09-15T18:05:00Z",
            value=4,
            product_id=1,
        )
        r_old = build_record(
            evidence_type="tournament_adoption",
            source="konami",
            source_id="ycs-2026-08-16",
            event_time="2026-08-16",
            publication_time="2026-08-16T18:00:00Z",
            first_seen_at="2026-08-16T18:05:00Z",
            value=3,
            product_id=1,
        )
        got = visible_at([r_early, r_old], "2026-09-14T23:59:59Z")
        self.assertEqual([r.source_id for r in got], ["ycs-2026-08-16"])


class VocabularyExposureTests(unittest.TestCase):
    def test_expected_evidence_types_present(self):
        for t in (
            "announcement",
            "tournament_adoption",
            "sales_velocity",
            "listing_supply",
            "reprint_notice",
            "banlist_change",
            "price_momentum",
            "price_snapshot",
        ):
            self.assertIn(t, EVIDENCE_TYPES)

    def test_expected_mapping_uncertainty_values_present(self):
        for v in (
            "exact",
            "resolved_by_name",
            "ambiguous",
            "unresolved",
            "not_a_card",
        ):
            self.assertIn(v, MAPPING_UNCERTAINTY_VALUES)


class TimestampParsingTests(unittest.TestCase):
    def test_bad_timestamp_is_rejected(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg", source_id="x",
                event_time="not-a-date",
                value=1, product_id=1,
            )

    def test_naive_iso_treated_as_utc(self):
        # An ISO string without a zone is treated as UTC (date-only
        # sources like article publish dates), never rejected.
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-2",
            publication_time="2026-09-10T15:00:00",
            value=1, product_id=1,
        )
        self.assertEqual(r.available_at, "2026-09-10T15:00:00Z")


class RevisionLeakRegressionTests(unittest.TestCase):
    """Regression tests for Point 2 of the PR-review feedback: a
    revision that retains the original article's publication_time must
    NOT become visible in an earlier retrospective replay window just
    because its publication_time falls inside it.
    """

    def test_revision_available_at_bounded_by_first_seen(self):
        original_pub = "2026-09-01T12:00:00Z"
        original = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time=original_pub,
            first_seen_at="2026-09-01T12:05:00Z",
            value=1, product_id=100,
            evidence={"note": "first read"},
        )
        # Original's available_at follows the pub time.
        self.assertEqual(original.available_at, original_pub)

        # A revised reading of the SAME article a week later, keeping
        # the source's stated publication_time. Without the revision
        # floor, this would land at 2026-09-01 and leak backward.
        revised = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time=original_pub,
            first_seen_at="2026-09-08T00:00:00Z",
            value=1, product_id=100,
            evidence={"note": "corrected"},
            supersedes=original.record_id,
        )
        self.assertEqual(revised.available_at, "2026-09-08T00:00:00Z")

    def test_visible_at_retrospective_hides_revision_before_first_seen(self):
        original_pub = "2026-09-01T12:00:00Z"
        original = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time=original_pub,
            first_seen_at="2026-09-01T12:05:00Z",
            value=1, product_id=100,
        )
        revised = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time=original_pub,
            first_seen_at="2026-09-08T00:00:00Z",
            value=1, product_id=100,
            supersedes=original.record_id,
        )
        # Retrospective replay at Sep 3: original is visible, revised
        # is not (its corrected content wasn't observed until Sep 8).
        got = visible_at([original, revised], "2026-09-03T00:00:00Z")
        self.assertIn(original.record_id, {r.record_id for r in got})
        self.assertNotIn(revised.record_id, {r.record_id for r in got})

        # Retrospective replay at Sep 10: both visible.
        got_later = visible_at([original, revised], "2026-09-10T00:00:00Z")
        self.assertEqual(
            {r.record_id for r in got_later},
            {original.record_id, revised.record_id},
        )


class ObservableAtLiveSimulationTests(unittest.TestCase):
    """Regression tests for Point 2: `observable_at` distinguishes
    public availability from our first observation, and
    `visible_at(mode="live_simulation")` uses it so live-alert
    simulation never assumes we could have acted before we crawled.
    """

    def test_observable_at_ignores_publication_time(self):
        # Source published on Sep 1; we didn't crawl until Sep 10.
        # observable_at must be Sep 10 (not Sep 1), because we could
        # not have alerted before we observed the source.
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-10T00:00:00Z",
            value=1, product_id=1,
        )
        self.assertEqual(r.available_at, "2026-09-01T00:00:00Z")
        self.assertEqual(r.observable_at, "2026-09-10T00:00:00Z")

    def test_live_simulation_mode_uses_observable_at(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            publication_time="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-10T00:00:00Z",
            value=1, product_id=1,
        )
        # Sep 5 retrospective: visible (public availability was Sep 1)
        retro = visible_at([r], "2026-09-05T00:00:00Z", mode="retrospective")
        self.assertEqual(len(retro), 1)
        # Sep 5 live-simulation: NOT visible (we hadn't crawled yet)
        live = visible_at([r], "2026-09-05T00:00:00Z", mode="live_simulation")
        self.assertEqual(live, [])
        # Sep 10 live-simulation: now visible.
        live_after = visible_at([r], "2026-09-10T00:00:00Z", mode="live_simulation")
        self.assertEqual(len(live_after), 1)

    def test_observable_at_still_respects_event_time(self):
        # A fact whose event is in the future is not actionable in
        # either mode before the event.
        r = build_record(
            evidence_type="banlist_change",
            source="konami", source_id="banlist-2026-10",
            event_time="2026-10-01T00:00:00Z",
            publication_time="2026-09-15T00:00:00Z",
            first_seen_at="2026-09-15T00:00:00Z",
            value=1, product_id=1,
        )
        self.assertEqual(r.observable_at, "2026-10-01T00:00:00Z")
        self.assertEqual(r.available_at, "2026-10-01T00:00:00Z")

    def test_unknown_visibility_mode_rejected(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-1",
            value=1, product_id=1,
        )
        with self.assertRaises(ValueError):
            visible_at([r], "2026-09-01T00:00:00Z", mode="not-a-mode")


class CardLevelIdentityTests(unittest.TestCase):
    """Regression tests for Point 3: card-level evidence is
    first-class; one announcement affecting many printings counts as
    one confirmation.
    """

    def test_card_level_record_accepts_card_key(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="ocg-cubic-2026-09",
            source_url="https://ygorganization.com/example",
            publication_time="2026-09-14T00:00:00Z",
            value=1,
            mapping_uncertainty="card_level",
            card_key="Cubic Wave",
            affected_printings=[111, 222, 333],
        )
        self.assertEqual(r.mapping_uncertainty, "card_level")
        self.assertEqual(r.card_key, "Cubic Wave")
        self.assertIsNone(r.product_id)
        self.assertEqual(r.affected_printings, (111, 222, 333))

    def test_card_level_record_accepts_archetype(self):
        r = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="ocg-cubic-support",
            publication_time="2026-09-14T00:00:00Z",
            value=1,
            mapping_uncertainty="card_level",
            archetype="Cubic",
            affected_printings=[111, 222],
        )
        self.assertEqual(r.archetype, "Cubic")

    def test_card_level_requires_card_key_or_archetype(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg", source_id="x",
                value=1,
                mapping_uncertainty="card_level",
            )

    def test_card_level_forbids_product_id(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg", source_id="x",
                value=1,
                mapping_uncertainty="card_level",
                card_key="Cubic Wave",
                product_id=111,
            )

    def test_affected_printings_only_allowed_on_card_level(self):
        # Setting affected_printings on a printing-level (`exact`)
        # record is a category error and must be rejected.
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg", source_id="x",
                value=1,
                product_id=111,
                affected_printings=[222],
            )

    def test_affected_printings_must_be_ints(self):
        with self.assertRaises(EvidenceValidationError):
            build_record(
                evidence_type="announcement",
                source="ygorg", source_id="x",
                value=1,
                mapping_uncertainty="card_level",
                card_key="Cubic Wave",
                affected_printings=["111"],  # str, not int
            )

    def test_one_card_level_record_is_one_confirmation(self):
        # Two different announcements about the same canonical card
        # are two records under distinct (source, source_id) keys, but
        # a single announcement fanning out to 5 printings is one
        # record under one key — never five.
        one = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="ocg-cubic-2026-09",
            publication_time="2026-09-14T00:00:00Z",
            value=1,
            mapping_uncertainty="card_level",
            card_key="Cubic Wave",
            affected_printings=[10, 20, 30, 40, 50],
        )
        two = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="ocg-cubic-2026-10",
            publication_time="2026-10-01T00:00:00Z",
            value=1,
            mapping_uncertainty="card_level",
            card_key="Cubic Wave",
            affected_printings=[10, 20, 30, 40, 50],
        )
        latest = latest_by_logical_key([one, two])
        self.assertEqual(len(latest), 2)
        # The five printings did not become five separate confirmations.
        self.assertNotIn(("ygorg", "printing-10"), latest)

    def test_ambiguous_is_distinct_from_card_level(self):
        # ambiguous = we could not resolve a printing-level reference.
        # card_level = the source itself was card/archetype-scoped.
        # Both forbid product_id but differ in meaning.
        ambiguous = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-a",
            value=1,
            mapping_uncertainty="ambiguous",
            card_name="Ambiguous Reference",
            evidence={"candidates": [1, 2, 3]},
        )
        card_level = build_record(
            evidence_type="announcement",
            source="ygorg", source_id="post-b",
            value=1,
            mapping_uncertainty="card_level",
            card_key="Deliberately Card-Scoped",
        )
        self.assertEqual(ambiguous.mapping_uncertainty, "ambiguous")
        self.assertEqual(card_level.mapping_uncertainty, "card_level")

    def test_card_level_round_trips_through_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "e.jsonl")
            r = build_record(
                evidence_type="announcement",
                source="ygorg", source_id="ocg-cubic-2026-09",
                publication_time="2026-09-14T00:00:00Z",
                value=1,
                mapping_uncertainty="card_level",
                card_key="Cubic Wave",
                archetype="Cubic",
                affected_printings=[111, 222, 333],
            )
            append_records(path, [r])
            got = list(iter_records(path))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].card_key, "Cubic Wave")
            self.assertEqual(got[0].archetype, "Cubic")
            self.assertEqual(got[0].affected_printings, (111, 222, 333))


if __name__ == "__main__":
    unittest.main()
