"""
Read-only coverage experiment for YGOPRODeck Tournament Meta Decks (TCG).

This intentionally does not write Meta Watch datasets or production databases.
"""
import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

YGOPRODECK_API_URL = "https://ygoprodeck.com/api/decks/getDecks.php"
TCG_CATEGORY = "Tournament Meta Decks"
WINDOW_DAYS = (14, 30, 90)
PAGE_SIZE = 20

_EXCLUDE_KEYWORDS = (
    "ocg",
    "master duel",
    "duel links",
    "genesys",
    "speed duel",
    "online",
    "remote",
    "duelingbook",
    "edo pro",
    "edopro",
    "test",
    "practice",
    "casual",
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


def run_experiment(output_path, lookback_days=90, timeout=30):
    from_date = (_now_utc() - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    rows = fetch_tcg_decks(from_date=from_date, timeout=timeout)
    report = build_coverage_report(rows)
    write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Run read-only YGOPRODeck Tournament Meta Decks (TCG) coverage experiment.")
    parser.add_argument("--output", required=True, help="Path to write sanitized coverage report JSON")
    parser.add_argument("--lookback-days", type=int, default=90, help="Lookback window in days")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    args = parser.parse_args()

    try:
        report = run_experiment(output_path=args.output, lookback_days=args.lookback_days, timeout=args.timeout)
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"YGOPRODeck coverage check failed: {exc}") from exc

    print(
        json.dumps(
            {
                "output": args.output,
                "events_found": report["events_found"],
                "last_14_days_events": report["coverage_windows"]["last_14_days"]["events"],
                "last_30_days_events": report["coverage_windows"]["last_30_days"]["events"],
                "last_90_days_events": report["coverage_windows"]["last_90_days"]["events"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
