"""
Read-only coverage experiment for YGOPRODeck Tournament Meta Decks (TCG).

This intentionally does not write Meta Watch datasets or production databases.
It never bypasses TLS, never uses private endpoints, never authenticates, and
paces detail-endpoint requests at no more than 1 per second.
"""
import argparse
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

YGOPRODECK_API_URL = "https://ygoprodeck.com/api/decks/getDecks.php"
YGOPRODECK_DECK_DETAIL_URL = "https://ygoprodeck.com/api/decks/get.php"
TCG_CATEGORY = "Tournament Meta Decks"
WINDOW_DAYS = (14, 30, 90)
PAGE_SIZE = 20
TOP_CUT_SAMPLE_SIZE = 20
DETAIL_MIN_INTERVAL_SECONDS = 1.0

POPULATION_TCG_ADVANCED = "TCG_ADVANCED"
POPULATION_OCG = "OCG"
POPULATION_MASTER_DUEL = "MASTER_DUEL"
POPULATION_RUSH_DUEL = "RUSH_DUEL"
POPULATION_GENESYS = "GENESYS"
POPULATION_OTHER = "OTHER"
_POPULATION_KEYWORDS = (
    (POPULATION_MASTER_DUEL, ("master duel",)),
    (POPULATION_RUSH_DUEL, ("rush duel",)),
    (POPULATION_OCG, ("ocg",)),
    (POPULATION_GENESYS, ("genesys",)),
    (
        POPULATION_OTHER,
        (
            "duel links",
            "speed duel",
            "online",
            "remote",
            "duelingbook",
            "edo pro",
            "edopro",
            "test",
            "practice",
            "casual",
        ),
    ),
)

_EXCLUDE_KEYWORDS = tuple(
    keyword
    for _bucket, keywords in _POPULATION_KEYWORDS
    for keyword in keywords
)
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_str(value):
    return value.strip() if isinstance(value, str) else ""


def _parse_json_array(raw):
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _has_external_link(record):
    return any(
        _URL_RE.search(_safe_str(record.get(field, "")))
        for field in ("deck_description", "deck_excerpt", "youtube_link")
    )


def _classify_deck_completeness(record):
    main = _parse_json_array(record.get("main_deck"))
    extra = _parse_json_array(record.get("extra_deck"))
    side = _parse_json_array(record.get("side_deck"))
    if main and extra and side:
        return "complete_structured", {"main_count": len(main), "extra_count": len(extra), "side_count": len(side)}
    if _has_external_link(record):
        return "external_link", {"main_count": len(main), "extra_count": len(extra), "side_count": len(side)}
    return "missing_or_incomplete", {"main_count": len(main), "extra_count": len(extra), "side_count": len(side)}


def _parse_event_date(value) -> Optional[datetime]:
    raw = _safe_str(value)
    if not raw:
        return None
    patterns = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d")
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    for parser in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _publication_timestamp_quality(value):
    raw = _safe_str(value)
    if not raw:
        return {"quality": "missing", "normalized": None}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return {"quality": "date_only", "normalized": raw}
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        if re.search(r"\bago\b", raw, re.IGNORECASE):
            return {"quality": "relative_text", "normalized": None}
        return {"quality": "unparseable", "normalized": None}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return {"quality": "timestamp", "normalized": _iso(parsed.astimezone(timezone.utc))}


def _is_paper_tcg_record(record):
    format_name = _safe_str(record.get("format")).lower()
    if format_name and "tournament meta decks" not in format_name:
        return False
    text_blob = " ".join(
        [
            _safe_str(record.get("format")),
            _safe_str(record.get("deck_name")),
            _safe_str(record.get("tournamentName")),
            _safe_str(record.get("deck_description")),
            _safe_str(record.get("deck_excerpt")),
        ]
    ).lower()
    return not any(token in text_blob for token in _EXCLUDE_KEYWORDS)


def _classify_population(record):
    """Assign a record to exactly one population bucket. Populations are kept
    completely separate: a record that matches any non-TCG keyword goes to that
    bucket, never to TCG_ADVANCED. Records with no marker default to
    TCG_ADVANCED (Tournament Meta Decks is a TCG-Advanced curated category)."""
    text_blob = " ".join(
        [
            _safe_str(record.get("format")),
            _safe_str(record.get("deck_name")),
            _safe_str(record.get("tournamentName")),
            _safe_str(record.get("deck_description")),
            _safe_str(record.get("deck_excerpt")),
        ]
    ).lower()
    for bucket, keywords in _POPULATION_KEYWORDS:
        if any(token in text_blob for token in keywords):
            return bucket
    return POPULATION_TCG_ADVANCED


def fetch_tcg_decks(from_date, timeout=30, session=None):
    client = session or requests.Session()
    offset = 0
    all_rows = []
    while True:
        response = client.get(
            YGOPRODECK_API_URL,
            params={
                "_sft_category": TCG_CATEGORY,
                "sort": "Updated",
                "from": from_date,
                "limit": PAGE_SIZE,
                "offset": offset,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("YGOPRODeck getDecks.php returned non-list JSON")
        if not page:
            break
        all_rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_rows


def build_coverage_report(rows, now=None):
    now = now or _now_utc()
    players_totals = {"complete_structured": 0, "external_link": 0, "missing_or_incomplete": 0}
    timestamp_quality_counts = defaultdict(int)
    excluded_count = 0

    by_event = {}
    for row in rows:
        if not _is_paper_tcg_record(row):
            excluded_count += 1
            continue

        event_name = _safe_str(row.get("tournamentName")) or "UNKNOWN_EVENT"
        event_date = _parse_event_date(row.get("submit_date"))
        event_key = (
            event_name.lower(),
            event_date.strftime("%Y-%m-%d") if event_date else _safe_str(row.get("submit_date")),
        )
        if event_key not in by_event:
            by_event[event_key] = {
                "event_name": event_name,
                "event_date": event_date.strftime("%Y-%m-%d") if event_date else None,
                "_event_dt": event_date,
                "participant_count_max": None,
                "players": {"complete_structured": 0, "external_link": 0, "missing_or_incomplete": 0},
                "deck_entries": 0,
                "distinct_players": set(),
                "sample_deck_urls": [],
            }

        entry = by_event[event_key]
        category, _counts = _classify_deck_completeness(row)
        entry["players"][category] += 1
        players_totals[category] += 1
        entry["deck_entries"] += 1

        player_name = _safe_str(row.get("tournamentPlayerName"))
        if player_name:
            entry["distinct_players"].add(player_name)

        participant_count = row.get("tournamentPlayerCount")
        if isinstance(participant_count, int):
            if entry["participant_count_max"] is None or participant_count > entry["participant_count_max"]:
                entry["participant_count_max"] = participant_count

        if row.get("pretty_url") and len(entry["sample_deck_urls"]) < 3:
            entry["sample_deck_urls"].append(f"https://ygoprodeck.com/deck/{row['pretty_url']}")

        quality = _publication_timestamp_quality(row.get("submit_date"))
        timestamp_quality_counts[quality["quality"]] += 1

    events = []
    for event in by_event.values():
        events.append(
            {
                "event_name": event["event_name"],
                "event_date": event["event_date"],
                "participant_count": event["participant_count_max"],
                "players": event["players"],
                "deck_entries": event["deck_entries"],
                "distinct_player_names": len(event["distinct_players"]),
                "sample_deck_urls": event["sample_deck_urls"],
            }
        )

    events.sort(
        key=lambda e: datetime.strptime(e["event_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if e["event_date"]
        else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    coverage_windows = {}
    for days in WINDOW_DAYS:
        cutoff = now - timedelta(days=days)
        window_events = [
            event
            for event in events
            if event["event_date"]
            and datetime.strptime(event["event_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc) > cutoff
        ]
        players = {"complete_structured": 0, "external_link": 0, "missing_or_incomplete": 0}
        for event in window_events:
            for k in players:
                players[k] += event["players"][k]
        coverage_windows[f"last_{days}_days"] = {"window_days": days, "events": len(window_events), "players": players}

    return {
        "generated_at": _iso(now),
        "query": {
            "source": "YGOPRODeck Tournament Meta Decks (TCG)",
            "endpoint": YGOPRODECK_API_URL,
            "filters": {
                "_sft_category": TCG_CATEGORY,
                "excluded_keywords": list(_EXCLUDE_KEYWORDS),
            },
        },
        "events_found": len(events),
        "excluded_non_paper_records": excluded_count,
        "overall_players": players_totals,
        "publication_timestamp_quality": dict(timestamp_quality_counts),
        "coverage_windows": coverage_windows,
        "events": events,
    }


def write_report(path, report):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def _extract_deck_id(record):
    for key in ("deck_id", "id"):
        value = record.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _extract_deck_url(record):
    pretty = _safe_str(record.get("pretty_url"))
    if pretty:
        return f"https://ygoprodeck.com/deck/{pretty}"
    deck_id = _extract_deck_id(record)
    if deck_id is not None:
        return f"https://ygoprodeck.com/deck/?id={deck_id}"
    return None


def _detail_card_array(detail, key):
    """Return the card array for main/extra/side from a detail payload, or
    None if the field is absent from the payload. Empty list is preserved
    verbatim (a legitimate empty Side deck must not be confused with an
    omitted field)."""
    if not isinstance(detail, dict):
        return None
    value = detail.get(key)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        parsed = _parse_json_array(value)
        # If the raw string is empty/whitespace, treat as absent, not empty.
        if not value.strip():
            return None
        return parsed
    return None


def fetch_deck_detail(
    deck_id,
    session=None,
    cache_dir=None,
    timeout=30,
    min_interval_seconds=DETAIL_MIN_INTERVAL_SECONDS,
    _last_call_ref=None,
):
    """Fetch a single deck detail payload with HTTPS verification, local file
    cache, and rate limiting. Never bypasses TLS. Never sends auth."""
    if deck_id is None:
        raise ValueError("deck_id is required")
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"deck_{deck_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                return json.load(f), True
    if _last_call_ref is not None and min_interval_seconds > 0:
        last = _last_call_ref.get("t")
        if last is not None:
            wait = min_interval_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
    client = session or requests.Session()
    response = client.get(
        YGOPRODECK_DECK_DETAIL_URL,
        params={"deck_id": deck_id},
        timeout=timeout,
    )
    if _last_call_ref is not None:
        _last_call_ref["t"] = time.monotonic()
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise ValueError(
            f"YGOPRODeck get.php returned non-object payload for deck_id={deck_id}"
        )
    if cache_path:
        with open(cache_path, "w") as f:
            json.dump(payload, f, sort_keys=True)
    return payload, False


def _classify_field(value, *, field_type="text"):
    """Return 'PASS' or 'MISSING'. Never invents data. Any non-empty
    well-typed value is a PASS; anything absent or empty is MISSING."""
    if field_type == "int":
        if isinstance(value, bool):  # bool is a subclass of int; reject
            return "MISSING"
        if isinstance(value, int):
            return "PASS"
        return "MISSING"
    if isinstance(value, str) and value.strip():
        return "PASS"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "PASS"
    return "MISSING"


def verify_deck_sample(list_row, detail_payload, *, observed_at, cache_hit=False):
    """Produce a sanitized per-deck verification row with explicit
    PASS / MISSING / FAIL states for every required field. Never fabricates
    tournament dates, placements, player counts, or publication timestamps."""
    if list_row is None:
        list_row = {}
    detail = detail_payload if isinstance(detail_payload, dict) else {}

    deck_id = _extract_deck_id(list_row) or _extract_deck_id(detail)
    deck_url = _extract_deck_url(list_row) or _extract_deck_url(detail)

    def _first_nonempty(*sources_and_keys):
        for source, key in sources_and_keys:
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    tournament_name = _first_nonempty(
        (list_row, "tournamentName"), (detail, "tournamentName")
    )
    player_name = _first_nonempty(
        (list_row, "tournamentPlayerName"), (detail, "tournamentPlayerName")
    )
    placement_raw = _first_nonempty(
        (list_row, "tournamentPlacement"),
        (detail, "tournamentPlacement"),
        (list_row, "placement"),
        (detail, "placement"),
    )
    player_count_raw = None
    for source in (list_row, detail):
        candidate = source.get("tournamentPlayerCount") if isinstance(source, dict) else None
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            player_count_raw = candidate
            break

    # Event date: only accept a value that actually parses. If the raw string
    # is present but unparseable, that is FAIL (junk data), not MISSING.
    raw_event_date = _safe_str(list_row.get("submit_date")) or _safe_str(
        detail.get("submit_date")
    )
    parsed_event_date = _parse_event_date(raw_event_date) if raw_event_date else None
    if not raw_event_date:
        event_date_status = "MISSING"
        event_date_iso = None
    elif parsed_event_date is None:
        event_date_status = "FAIL"
        event_date_iso = None
    else:
        event_date_status = "PASS"
        event_date_iso = parsed_event_date.strftime("%Y-%m-%d")

    # Card arrays: use detail payload only. Absent field -> FAIL, empty list
    # -> MISSING for the "non-empty" check (empty side is legitimate).
    main_arr = _detail_card_array(detail, "main_deck")
    extra_arr = _detail_card_array(detail, "extra_deck")
    side_arr = _detail_card_array(detail, "side_deck")

    def _array_present(arr):
        if arr is None:
            return "FAIL"
        return "PASS"

    def _array_nonempty(arr):
        if arr is None:
            return "FAIL"
        if len(arr) == 0:
            return "MISSING"
        return "PASS"

    main_present = _array_present(main_arr)
    extra_present = _array_present(extra_arr)
    side_present = _array_present(side_arr)
    main_nonempty = _array_nonempty(main_arr)
    extra_nonempty = _array_nonempty(extra_arr)
    side_nonempty = _array_nonempty(side_arr)

    # Structure complete: all three arrays present, main and extra non-empty.
    # An empty side deck is legitimate and does not fail this check.
    if (
        main_present == "PASS"
        and extra_present == "PASS"
        and side_present == "PASS"
        and main_nonempty == "PASS"
        and extra_nonempty == "PASS"
    ):
        structure_complete = "PASS"
    elif "FAIL" in (main_present, extra_present, side_present):
        structure_complete = "FAIL"
    else:
        structure_complete = "MISSING"

    population = _classify_population({**list_row, **{k: v for k, v in detail.items() if k not in list_row}})
    tcg_classification = "PASS" if population == POPULATION_TCG_ADVANCED else "FAIL"

    publication_quality = _publication_timestamp_quality(raw_event_date)

    return {
        "deck_id": deck_id,
        "deck_url": deck_url,
        "deck_id_status": "PASS" if deck_id is not None else "FAIL",
        "deck_url_status": "PASS" if deck_url else "MISSING",
        "population_bucket": population,
        "tcg_classification": tcg_classification,
        "tournament_name": tournament_name,
        "tournament_name_status": _classify_field(tournament_name),
        "event_date": event_date_iso,
        "event_date_status": event_date_status,
        "event_date_publication_quality": publication_quality["quality"],
        "player_name_status": _classify_field(player_name),
        "placement_status": _classify_field(placement_raw),
        "player_count": player_count_raw,
        "player_count_status": _classify_field(player_count_raw, field_type="int"),
        "observed_at": observed_at,
        "observed_at_status": "PASS",
        "main_deck_present_status": main_present,
        "main_deck_nonempty_status": main_nonempty,
        "main_deck_count": len(main_arr) if main_arr is not None else None,
        "extra_deck_present_status": extra_present,
        "extra_deck_nonempty_status": extra_nonempty,
        "extra_deck_count": len(extra_arr) if extra_arr is not None else None,
        "side_deck_present_status": side_present,
        "side_deck_nonempty_status": side_nonempty,
        "side_deck_count": len(side_arr) if side_arr is not None else None,
        "structure_complete_status": structure_complete,
        "detail_cache_hit": bool(cache_hit),
        "sample_kind": "top_cut_only",
    }


_VERIFIED_STATUS_FIELDS = (
    "deck_id_status",
    "deck_url_status",
    "tcg_classification",
    "tournament_name_status",
    "event_date_status",
    "player_name_status",
    "placement_status",
    "player_count_status",
    "observed_at_status",
    "main_deck_present_status",
    "main_deck_nonempty_status",
    "extra_deck_present_status",
    "extra_deck_nonempty_status",
    "side_deck_present_status",
    "side_deck_nonempty_status",
    "structure_complete_status",
)


def _select_recent_paper_rows(rows, sample_size):
    candidates = []
    for row in rows:
        if not _is_paper_tcg_record(row):
            continue
        parsed = _parse_event_date(row.get("submit_date"))
        if parsed is None:
            # Rows without a parseable date are still eligible for sampling
            # (they will be recorded FAIL for event_date), but sort last.
            sort_key = datetime.min.replace(tzinfo=timezone.utc)
        else:
            sort_key = parsed
        deck_id = _extract_deck_id(row)
        if deck_id is None:
            continue
        candidates.append((sort_key, deck_id, row))
    # Sort newest first; break ties by deck_id descending for determinism.
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    # De-duplicate by deck_id, preserving first (most recent) occurrence.
    seen = set()
    selected = []
    for _sort_key, deck_id, row in candidates:
        if deck_id in seen:
            continue
        seen.add(deck_id)
        selected.append(row)
        if len(selected) >= sample_size:
            break
    return selected


def sample_top_cut_details(
    rows,
    *,
    sample_size=TOP_CUT_SAMPLE_SIZE,
    session=None,
    cache_dir=None,
    timeout=30,
    min_interval_seconds=DETAIL_MIN_INTERVAL_SECONDS,
    now=None,
):
    """Follow deck-detail links for the N most-recent paper TCG list rows and
    verify each one. Returns a sanitized report block with per-deck rows and
    PASS / MISSING / FAIL totals for every checked field."""
    now = now or _now_utc()
    observed_at = _iso(now)
    selected = _select_recent_paper_rows(rows, sample_size)

    last_call_ref = {"t": None}
    details = []
    errors = []
    for row in selected:
        deck_id = _extract_deck_id(row)
        try:
            detail_payload, cache_hit = fetch_deck_detail(
                deck_id,
                session=session,
                cache_dir=cache_dir,
                timeout=timeout,
                min_interval_seconds=min_interval_seconds,
                _last_call_ref=last_call_ref,
            )
        except (requests.RequestException, ValueError) as exc:
            errors.append({"deck_id": deck_id, "error": str(exc)})
            details.append(
                verify_deck_sample(
                    row,
                    {},
                    observed_at=observed_at,
                    cache_hit=False,
                )
            )
            continue
        details.append(
            verify_deck_sample(
                row,
                detail_payload,
                observed_at=observed_at,
                cache_hit=cache_hit,
            )
        )

    totals = defaultdict(lambda: {"PASS": 0, "MISSING": 0, "FAIL": 0})
    populations = defaultdict(int)
    for record in details:
        populations[record["population_bucket"]] += 1
        for field in _VERIFIED_STATUS_FIELDS:
            state = record.get(field, "MISSING")
            if state not in ("PASS", "MISSING", "FAIL"):
                state = "MISSING"
            totals[field][state] += 1

    return {
        "sample_size_target": sample_size,
        "sample_size_actual": len(details),
        "sample_kind": "top_cut_only",
        "endpoint": YGOPRODECK_DECK_DETAIL_URL,
        "min_interval_seconds": float(min_interval_seconds),
        "observed_at": observed_at,
        "population_bucket_counts": dict(populations),
        "field_totals": {field: dict(counts) for field, counts in totals.items()},
        "decks": details,
        "detail_errors": errors,
    }


def run_experiment(
    output_path,
    lookback_days=90,
    timeout=30,
    sample_size=TOP_CUT_SAMPLE_SIZE,
    cache_dir=None,
    min_interval_seconds=DETAIL_MIN_INTERVAL_SECONDS,
    session=None,
):
    from_date = (_now_utc() - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    client = session or requests.Session()
    rows = fetch_tcg_decks(from_date=from_date, timeout=timeout, session=client)
    report = build_coverage_report(rows)
    sample_block = sample_top_cut_details(
        rows,
        sample_size=sample_size,
        session=client,
        cache_dir=cache_dir,
        timeout=timeout,
        min_interval_seconds=min_interval_seconds,
    )
    report["top_cut_sample"] = sample_block
    write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Run read-only YGOPRODeck Tournament Meta Decks (TCG) coverage experiment."
    )
    parser.add_argument("--output", required=True, help="Path to write sanitized coverage report JSON")
    parser.add_argument("--lookback-days", type=int, default=90, help="Lookback window in days")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=TOP_CUT_SAMPLE_SIZE,
        help="Number of recent Tournament Meta Decks (TCG) records to follow to the detail endpoint",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Local directory for caching deck detail JSON payloads",
    )
    parser.add_argument(
        "--min-interval-seconds",
        type=float,
        default=DETAIL_MIN_INTERVAL_SECONDS,
        help="Minimum seconds between detail-endpoint calls (>=1.0 keeps well under the 20 req/s limit)",
    )
    args = parser.parse_args()

    if args.min_interval_seconds < DETAIL_MIN_INTERVAL_SECONDS:
        raise SystemExit(
            f"--min-interval-seconds must be >= {DETAIL_MIN_INTERVAL_SECONDS} to respect the documented rate limit"
        )

    try:
        report = run_experiment(
            output_path=args.output,
            lookback_days=args.lookback_days,
            timeout=args.timeout,
            sample_size=args.sample_size,
            cache_dir=args.cache_dir,
            min_interval_seconds=args.min_interval_seconds,
        )
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"YGOPRODeck coverage check failed: {exc}") from exc

    sample_block = report.get("top_cut_sample", {}) or {}
    print(
        json.dumps(
            {
                "output": args.output,
                "events_found": report["events_found"],
                "last_14_days_events": report["coverage_windows"]["last_14_days"]["events"],
                "last_30_days_events": report["coverage_windows"]["last_30_days"]["events"],
                "last_90_days_events": report["coverage_windows"]["last_90_days"]["events"],
                "top_cut_sample_size_actual": sample_block.get("sample_size_actual", 0),
                "top_cut_structure_complete_totals": sample_block.get("field_totals", {}).get(
                    "structure_complete_status", {}
                ),
                "top_cut_population_bucket_counts": sample_block.get("population_bucket_counts", {}),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
