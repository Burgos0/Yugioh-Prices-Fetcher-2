"""
Unified evidence layer for the combined prediction system.

This module defines a single canonical record shape that every future
input source (Konami/YGOrganization announcements, TopDeck tournament
adoption, TCGCSV/TCGPlayer sales & supply, banlist changes, reprint
notices, momentum signals) is expected to emit. Downstream code
combines evidence across sources on this shared shape rather than on
each source's native fields.

Design invariants (see PROJECT_STATUS.md, section A "Unified evidence
layer"):

* Missing information is UNKNOWN, not zero. `value` may be `None`; a
  `missing_reason` string explains why. Callers must not silently
  substitute 0 / "" / "N/A" for a genuinely unknown observation.

* Timestamps are separated by role and are never substituted for one
  another:
    - `event_time`        : when the underlying event happened
                            (tournament played, announcement effective,
                            price snapshot dated).
    - `publication_time`  : when the source first *published* the fact
                            (article date, standings publish date).
                            May be None; callers must NOT fill it with
                            event_time.
    - `first_seen_at`     : when *this repository* first observed the
                            record. Always populated on write.
    - `available_at`      : the earliest timestamp at which a downstream
                            evaluator could have acted on this record.
                            Typically max(publication_time or
                            first_seen_at, event_time). Used for
                            time-ordered / point-in-time evaluation so
                            historical replays cannot "see the future".

* Revisions do not overwrite history. A newer observation of the same
  logical fact is appended as a new record with `supersedes` pointing
  at the earlier `record_id`. Historical evaluation replays only
  records whose `available_at` is <= the replay cutoff.

* `product_id` (TCGPlayer product id, matching the tracked printing
  key already used by `prices.db` and `top_gainers.json`) is the
  primary printing identity. `card_name`/`set_name` are provenance
  hints, never the join key. `mapping_uncertainty` describes any
  fuzziness in how the source got mapped to a `product_id`.

The storage format is line-delimited JSON (JSONL), append-only. This
keeps the file trivially auditable, safe under concurrent readers,
and compatible with the existing snapshot backend (each new file is
just another artifact in the snapshot manifest — see
scripts/snapshot_storage.py).

This module does NOT perform any network I/O and does NOT read
prices.db. It is a pure schema + storage helper so that unit tests can
exercise it without Flask / SQLite / requests. Source collectors
(added in follow-up PRs) will construct EvidenceRecord instances and
call `append_records` to persist them.
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Optional


SCHEMA_VERSION = 1

# Fixed vocabulary for `evidence_type`. Downstream signal combination
# uses this to weight/filter records; adding a new source means adding a
# new entry here and adjusting the combiner, not re-shaping every
# record. Keep in sync with docs/PROJECT_STATUS.md.
EVIDENCE_TYPES = frozenset({
    "announcement",         # new-card / archetype-support announcement
    "tournament_adoption",  # decklist appearance / usage count
    "sales_velocity",       # completed sales rate
    "listing_supply",       # active listings, seller count
    "reprint_notice",       # confirmed reprint / new set inclusion
    "banlist_change",       # forbidden/limited/semi-limited change
    "price_momentum",       # existing Early Movers / Top Gainers signal
    "price_snapshot",       # daily tracked price observation
})

# Fixed vocabulary for `mapping_uncertainty`. `exact` means the source
# already carried a TCGPlayer product id; `resolved_by_name` means the
# name matched a unique tracked printing; `ambiguous` means multiple
# printings match (record all candidate ids in `evidence.candidates`);
# `unresolved` means the referenced card exists in the game but has no
# tracked printing in prices.db; `not_a_card` means the source referred
# to something other than a card (event, article, reprint set, ...).
MAPPING_UNCERTAINTY_VALUES = frozenset({
    "exact",
    "resolved_by_name",
    "ambiguous",
    "unresolved",
    "not_a_card",
})


class EvidenceValidationError(ValueError):
    """Raised when an EvidenceRecord fails schema validation."""


@dataclass(frozen=True)
class EvidenceRecord:
    """One observation of one fact from one source.

    See module docstring for the meaning of each field. All timestamps
    are ISO-8601 strings in UTC (`YYYY-MM-DDTHH:MM:SSZ` or
    `YYYY-MM-DD` for date-only sources). Use :func:`build_record` to
    construct records with default `record_id` / `first_seen_at` /
    `available_at`.
    """

    # Identity
    record_id: str
    schema_version: int
    evidence_type: str

    # Source provenance
    source: str
    source_id: str
    source_url: Optional[str]

    # Time
    event_time: Optional[str]
    publication_time: Optional[str]
    first_seen_at: str
    available_at: str

    # Value + missingness
    value: Any
    value_unit: Optional[str]
    freshness_days: Optional[float]
    missing_reason: Optional[str]

    # Printing target
    product_id: Optional[int]
    card_name: Optional[str]
    set_name: Optional[str]
    mapping_uncertainty: str

    # Free-form supporting evidence and revision chaining
    evidence: Mapping[str, Any] = field(default_factory=dict)
    supersedes: Optional[str] = None

    def to_json_dict(self) -> dict:
        """Return a JSON-serializable dict (evidence is copied)."""
        d = dataclasses.asdict(self)
        d["evidence"] = dict(self.evidence)
        return d


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp or a `YYYY-MM-DD` date.

    Raises EvidenceValidationError on failure. Returns a
    timezone-aware UTC datetime; date-only inputs are treated as
    midnight UTC (they represent whole-day observations).
    """
    if not isinstance(ts, str) or not ts:
        raise EvidenceValidationError(f"expected ISO timestamp, got {ts!r}")
    candidate = ts.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvidenceValidationError(f"invalid ISO timestamp {ts!r}: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def compute_available_at(
    event_time: Optional[str],
    publication_time: Optional[str],
    first_seen_at: str,
) -> str:
    """Return the earliest timestamp at which a downstream evaluator
    could act on this record.

    Rules:
    * If ``publication_time`` is known, the fact could have been acted
      on no earlier than the later of ``publication_time`` and
      ``event_time``.
    * If ``publication_time`` is unknown, ``first_seen_at`` is used in
      its place (we cannot claim to have known a fact before we
      observed it). ``event_time`` is never used as a substitute for
      publication time — that would falsify replay timing for facts
      that were published later than the event they describe.
    """
    seen = _parse_iso(first_seen_at)
    ev = _parse_iso(event_time) if event_time else None
    pub = _parse_iso(publication_time) if publication_time else None
    lower_bound = pub if pub is not None else seen
    candidates = [lower_bound]
    if ev is not None:
        candidates.append(ev)
    chosen = max(candidates)
    return chosen.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_record(
    *,
    evidence_type: str,
    source: str,
    source_id: str,
    source_url: Optional[str] = None,
    event_time: Optional[str] = None,
    publication_time: Optional[str] = None,
    first_seen_at: Optional[str] = None,
    value: Any = None,
    value_unit: Optional[str] = None,
    freshness_days: Optional[float] = None,
    missing_reason: Optional[str] = None,
    product_id: Optional[int] = None,
    card_name: Optional[str] = None,
    set_name: Optional[str] = None,
    mapping_uncertainty: str = "exact",
    evidence: Optional[Mapping[str, Any]] = None,
    supersedes: Optional[str] = None,
    record_id: Optional[str] = None,
) -> EvidenceRecord:
    """Build a validated :class:`EvidenceRecord`.

    ``first_seen_at`` defaults to now (UTC); ``available_at`` is
    computed via :func:`compute_available_at` and cannot be passed in
    directly, since it is a derived, replay-critical field.
    """
    seen = first_seen_at or _utc_now_iso()
    _parse_iso(seen)  # validate now, before we compute anything
    record = EvidenceRecord(
        record_id=record_id or str(uuid.uuid4()),
        schema_version=SCHEMA_VERSION,
        evidence_type=evidence_type,
        source=source,
        source_id=source_id,
        source_url=source_url,
        event_time=event_time,
        publication_time=publication_time,
        first_seen_at=seen,
        available_at=compute_available_at(event_time, publication_time, seen),
        value=value,
        value_unit=value_unit,
        freshness_days=freshness_days,
        missing_reason=missing_reason,
        product_id=product_id,
        card_name=card_name,
        set_name=set_name,
        mapping_uncertainty=mapping_uncertainty,
        evidence=dict(evidence or {}),
        supersedes=supersedes,
    )
    validate_record(record)
    return record


def validate_record(record: EvidenceRecord) -> None:
    """Raise :class:`EvidenceValidationError` if ``record`` violates
    the shared invariants documented at the top of this module.

    The important cross-field checks (beyond simple type checks):

    * ``evidence_type`` and ``mapping_uncertainty`` are drawn from the
      fixed vocabularies above.
    * A missing value must carry a ``missing_reason``; a present value
      must NOT carry one (otherwise "missing" and "zero" would both be
      representable, defeating the invariant).
    * All timestamps parse; ``available_at`` matches
      :func:`compute_available_at` (this is what makes replays
      trustworthy).
    * A mapping marked ``exact`` requires a ``product_id``.
    * A record marked ``unresolved`` must NOT carry a ``product_id``
      (otherwise the caller is claiming both "I don't know which
      printing" and "here is the printing").
    """
    if record.schema_version != SCHEMA_VERSION:
        raise EvidenceValidationError(
            f"schema_version {record.schema_version} != current {SCHEMA_VERSION}"
        )
    if record.evidence_type not in EVIDENCE_TYPES:
        raise EvidenceValidationError(
            f"unknown evidence_type {record.evidence_type!r}; "
            f"expected one of {sorted(EVIDENCE_TYPES)}"
        )
    if record.mapping_uncertainty not in MAPPING_UNCERTAINTY_VALUES:
        raise EvidenceValidationError(
            f"unknown mapping_uncertainty {record.mapping_uncertainty!r}; "
            f"expected one of {sorted(MAPPING_UNCERTAINTY_VALUES)}"
        )
    if not record.source or not record.source_id:
        raise EvidenceValidationError("source and source_id are required")

    for field_name in ("event_time", "publication_time"):
        value = getattr(record, field_name)
        if value is not None:
            _parse_iso(value)

    # first_seen_at and available_at are required and must parse
    _parse_iso(record.first_seen_at)
    _parse_iso(record.available_at)
    expected = compute_available_at(
        record.event_time, record.publication_time, record.first_seen_at
    )
    if record.available_at != expected:
        raise EvidenceValidationError(
            f"available_at {record.available_at!r} does not match "
            f"computed {expected!r}; do not construct records manually — "
            f"use build_record()"
        )

    is_missing = record.value is None
    if is_missing and not record.missing_reason:
        raise EvidenceValidationError(
            "value is None but missing_reason is not set; a genuinely "
            "unknown observation must explain why"
        )
    if not is_missing and record.missing_reason:
        raise EvidenceValidationError(
            "missing_reason is set but value is not None; missing and "
            "present are mutually exclusive"
        )

    if record.mapping_uncertainty == "exact" and record.product_id is None:
        raise EvidenceValidationError(
            "mapping_uncertainty='exact' requires a product_id"
        )
    if record.mapping_uncertainty == "unresolved" and record.product_id is not None:
        raise EvidenceValidationError(
            "mapping_uncertainty='unresolved' cannot carry a product_id"
        )
    if record.product_id is not None and not isinstance(record.product_id, int):
        raise EvidenceValidationError(
            f"product_id must be int, got {type(record.product_id).__name__}"
        )
    if record.freshness_days is not None and record.freshness_days < 0:
        raise EvidenceValidationError(
            f"freshness_days must be >= 0, got {record.freshness_days!r}"
        )


def append_records(path: str, records: Iterable[EvidenceRecord]) -> int:
    """Append validated records to a JSONL file, creating parents as
    needed. Returns the count written.

    Existing content is never rewritten. Callers wanting to publish a
    revised value for the same logical fact must build a new record
    whose ``supersedes`` points at the earlier ``record_id``; the
    historical record stays in the file.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    count = 0
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            validate_record(record)
            f.write(json.dumps(record.to_json_dict(), sort_keys=True))
            f.write("\n")
            count += 1
    return count


def iter_records(path: str) -> Iterator[EvidenceRecord]:
    """Yield every record in ``path`` in file order (i.e. append order).

    Silently skips blank lines. Raises :class:`EvidenceValidationError`
    on any malformed line so corrupt input is never returned as if it
    were a valid record.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvidenceValidationError(
                    f"{path}:{line_no}: malformed JSON: {exc}"
                ) from exc
            try:
                record = EvidenceRecord(**obj)
            except TypeError as exc:
                raise EvidenceValidationError(
                    f"{path}:{line_no}: record missing required field: {exc}"
                ) from exc
            validate_record(record)
            yield record


def latest_by_logical_key(records: Iterable[EvidenceRecord]) -> dict:
    """Reduce a stream of records to the latest revision per
    (source, source_id) pair, following ``supersedes`` chains.

    Returned dict maps (source, source_id) -> EvidenceRecord (the tail
    of the supersedes chain, i.e. the current version). Records that
    are named as ``supersedes`` targets are excluded from the result
    even if they themselves have no successor in the input, because
    the caller has explicitly retired them.
    """
    by_id: dict[str, EvidenceRecord] = {}
    superseded: set[str] = set()
    for r in records:
        by_id[r.record_id] = r
        if r.supersedes:
            superseded.add(r.supersedes)
    latest: dict[tuple[str, str], EvidenceRecord] = {}
    for r in by_id.values():
        if r.record_id in superseded:
            continue
        key = (r.source, r.source_id)
        prev = latest.get(key)
        if prev is None or r.first_seen_at > prev.first_seen_at:
            latest[key] = r
    return latest


def visible_at(records: Iterable[EvidenceRecord], cutoff_iso: str) -> list:
    """Return records whose ``available_at`` is <= ``cutoff_iso``.

    This is the point-in-time filter used by historical evaluation so
    a backtest at time T can never accidentally consult a record that
    was not yet available at T.
    """
    cutoff = _parse_iso(cutoff_iso)
    out = []
    for r in records:
        if _parse_iso(r.available_at) <= cutoff:
            out.append(r)
    return out
