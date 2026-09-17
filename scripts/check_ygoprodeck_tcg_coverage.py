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
# Ordered candidate detail URLs. `/api/decks/get.php` was the historically
# documented endpoint, but the current live probe run returned HTTP 404 for
# every request. `/api/decks/getDeck.php` (singular) and the frontend
# `/api/deck.php?decklist=` are known variants used by the ygoprodeck.com
# front-end; we try them in order and record per-URL HTTP status counts in
# a sanitized diagnostic block so future breaks are self-diagnosing.
YGOPRODECK_DECK_DETAIL_URL_CANDIDATES = (
    ("https://ygoprodeck.com/api/decks/getDeck.php", "deck_id"),
    ("https://ygoprodeck.com/api/deck.php", "decklist"),
    ("https://ygoprodeck.com/api/decks/get.php", "deck_id"),
)
# Legacy single-URL alias kept for callers that still import it.
YGOPRODECK_DECK_DETAIL_URL = YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0]
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

# Multiple observed field-name variants used by getDecks.php over time. The
# probe tries each in order and uses the first one that yields a usable value.
# We never fall back to a *value* — only to an alternate key that carries the
# same semantic field. A missing/absent field stays MISSING; an unparseable
# value stays FAIL.
DECK_ID_KEYS = ("deckNum", "deckID", "deck_id", "id")
EVENT_DATE_KEYS = (
    "submit_date",
    "date_submitted",
    "dateSubmitted",
    "date_created",
    "dateCreated",
    "created",
    "date",
    "updated",
)
PRETTY_URL_KEYS = ("pretty_url", "prettyURL", "url_slug", "slug")

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
        # Not JSON — some getDecks.php rows use comma-separated card IDs,
        # e.g. "12345,67890,42". Accept that shape as a legitimate array
        # source; anything else stays empty.
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens and all(re.fullmatch(r"-?\d+", t) for t in tokens):
            return tokens
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


_EVENT_DATE_STRPTIME_PATTERNS = (
    # Date-only.
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    # MySQL DATETIME (space separator) — the format actually returned by
    # ygoprodeck.com/api/decks/getDecks.php `submit_date`, e.g.
    # "2023-11-27 17:42:36". Python 3.11's fromisoformat also accepts this,
    # but we register it explicitly so behaviour is stable across runners.
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    # European/US variants with time.
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    # Human-readable variants sometimes surfaced by WP-backed feeds.
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
)


_RELATIVE_AGO_UNITS = {
    "minute": timedelta(minutes=1),
    "minutes": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hours": timedelta(hours=1),
    "day": timedelta(days=1),
    "days": timedelta(days=1),
    "week": timedelta(weeks=1),
    "weeks": timedelta(weeks=1),
    "month": timedelta(days=30),
    "months": timedelta(days=30),
    "year": timedelta(days=365),
    "years": timedelta(days=365),
}
_RELATIVE_AGO_RE = re.compile(
    r"^\s*(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago\s*$",
    re.IGNORECASE,
)


def _parse_relative_ago(raw: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a *concrete* relative timestamp such as `3 days ago`,
    `1 week ago`, `yesterday`, or `today`. Vague forms like `recently`
    or `a while ago` return None so they stay honestly FAIL.

    Not a substitution from another field — this parses the same source
    value the API sent us, just decoding its relative encoding.
    Approximations (weeks/months/years) are still returned so that
    14/30/90-day coverage windows work; the sanitized diagnostic
    surfaces `relative_ago` counts separately so consumers know the
    precision level."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    ref = now or _now_utc()
    if text == "today" or text == "just now":
        return ref
    if text == "yesterday":
        return ref - timedelta(days=1)
    match = _RELATIVE_AGO_RE.match(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = _RELATIVE_AGO_UNITS[unit] * amount
    return ref - delta


def _parse_event_date(value, *, now: Optional[datetime] = None) -> Optional[datetime]:
    # Numeric epoch (int / float / all-digit string).
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _parse_epoch(value)
    raw = _safe_str(value)
    if not raw:
        return None
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        try:
            return _parse_epoch(int(raw))
        except (TypeError, ValueError):
            pass
    for pattern in _EVENT_DATE_STRPTIME_PATTERNS:
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Relative-ago phrases such as `3 days ago`. Only concrete "N units
    # ago" (and `today`/`yesterday`) parse; vague strings stay FAIL.
    return _parse_relative_ago(raw, now=now)


def _parse_epoch(number) -> Optional[datetime]:
    """Interpret an integer/float as a Unix epoch. 10-digit values are
    treated as seconds, 13-digit as milliseconds. Rejects implausible
    years (< 2000 or > 2100) to guard against non-epoch integers."""
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None
    if abs(n) >= 10**12:  # 13-digit ms
        n = n // 1000
    if abs(n) < 10**9 or abs(n) > 10**10:
        return None
    try:
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if dt.year < 2000 or dt.year > 2100:
        return None
    return dt


_EVENT_DATE_SHAPE_PATTERNS = (
    ("date_only", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("sql_datetime", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$")),
    ("iso_t", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$")),
    ("us_slash", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}(?: \d{1,2}:\d{2}(?::\d{2})?)?$")),
    ("eu_dash", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}(?: \d{1,2}:\d{2}(?::\d{2})?)?$")),
    ("epoch10", re.compile(r"^-?\d{10}$")),
    ("epoch13", re.compile(r"^-?\d{13}$")),
    ("digits_other", re.compile(r"^-?\d+$")),
    ("human_month", re.compile(r"^(?:\d{1,2}\s+)?[A-Za-z]{3,9}\s+\d{1,2}(?:,)?\s+\d{4}$")),
    ("relative_ago", re.compile(r"\b(ago|yesterday|today|just now)\b", re.IGNORECASE)),
)


def _classify_event_date_shape(value) -> str:
    """Regex-only shape classification of a raw date value. Never returns
    the value itself — only a stable category name. Used for sanitized
    diagnostics so unknown formats can be identified without leaking
    tournament data."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        digits = str(abs(int(value)))
        if len(digits) == 10:
            return "epoch10"
        if len(digits) == 13:
            return "epoch13"
        return "numeric_other"
    raw = _safe_str(value)
    if not raw:
        return "empty"
    for name, pattern in _EVENT_DATE_SHAPE_PATTERNS:
        if pattern.search(raw) if name == "relative_ago" else pattern.match(raw):
            return name
    return "unknown"


def _publication_timestamp_quality(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = _parse_epoch(value)
        if parsed is None:
            return {"quality": "unparseable", "normalized": None}
        return {"quality": "timestamp", "normalized": _iso(parsed)}
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
        event_date, raw_event_date, _date_key, _parse_ok = _extract_event_date(row)
        event_key = (
            event_name.lower(),
            event_date.strftime("%Y-%m-%d") if event_date else (raw_event_date or ""),
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

        deck_url = _extract_deck_url(row)
        if deck_url and len(entry["sample_deck_urls"]) < 3:
            entry["sample_deck_urls"].append(deck_url)

        raw_pub, _pub_key = _extract_event_date_raw(row)
        quality = _publication_timestamp_quality(raw_pub)
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
    if not isinstance(record, dict):
        return None
    for key in DECK_ID_KEYS:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _extract_deck_url(record):
    if not isinstance(record, dict):
        return None
    for key in PRETTY_URL_KEYS:
        pretty = _safe_str(record.get(key))
        if pretty:
            return f"https://ygoprodeck.com/deck/{pretty}"
    deck_id = _extract_deck_id(record)
    if deck_id is not None:
        return f"https://ygoprodeck.com/deck/?id={deck_id}"
    return None


def _extract_event_date_raw(record):
    """Return (raw_value, key_used) for the first candidate date key that
    holds a non-empty value, or (None, None) if none present. Never
    substitutes a value from another field-shape. Raw value may be a
    string or a numeric epoch — parsing normalises it later."""
    if not isinstance(record, dict):
        return None, None
    for key in EVENT_DATE_KEYS:
        raw_value = record.get(key)
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped:
                return stripped, key
        elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return raw_value, key
    return None, None


def _extract_event_date(record, *, now=None):
    """Return (parsed_datetime, raw_string, key_used, parse_ok)."""
    raw, key = _extract_event_date_raw(record)
    if raw is None:
        return None, None, None, False
    parsed = _parse_event_date(raw, now=now)
    return parsed, raw, key, parsed is not None


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
    _http_status_counter=None,
):
    """Fetch a single deck detail payload with HTTPS verification, local file
    cache, and rate limiting. Never bypasses TLS. Never sends auth.

    Tries each URL in ``YGOPRODECK_DECK_DETAIL_URL_CANDIDATES`` in order and
    accepts the first response that:
      * returns HTTP 200 with a JSON body, AND
      * whose body is a dict (or a list-of-one dict) that exposes any of the
        expected deck-array keys (`main_deck`, `extra_deck`, `side_deck`).

    Per-URL HTTP status codes are recorded in ``_http_status_counter`` (a
    ``defaultdict(Counter)`` keyed by URL) so the caller can surface them
    in a sanitized diagnostic. Only status codes and URLs are recorded —
    never response bodies.

    If every candidate fails, raises :class:`requests.RequestException` with
    a summary of the tried URLs and their statuses (URL + status only).
    """
    if deck_id is None:
        raise ValueError("deck_id is required")
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"deck_{deck_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                return json.load(f), True
    client = session or requests.Session()
    last_error = None
    tried = []
    for url, param_name in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES:
        # Rate-limit between EVERY outbound request (candidate attempts count
        # too — we never burst-hit the origin).
        if _last_call_ref is not None and min_interval_seconds > 0:
            last = _last_call_ref.get("t")
            if last is not None:
                wait = min_interval_seconds - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
        try:
            response = client.get(
                url,
                params={param_name: deck_id},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if _last_call_ref is not None:
                _last_call_ref["t"] = time.monotonic()
            last_error = exc
            tried.append((url, "network_error"))
            if _http_status_counter is not None:
                _http_status_counter[url]["network_error"] = (
                    _http_status_counter[url].get("network_error", 0) + 1
                )
            continue
        if _last_call_ref is not None:
            _last_call_ref["t"] = time.monotonic()
        status = getattr(response, "status_code", None)
        if _http_status_counter is not None and status is not None:
            _http_status_counter[url][status] = (
                _http_status_counter[url].get(status, 0) + 1
            )
        tried.append((url, status))
        if status != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            continue
        # Require at least one recognized deck-structure key so we don't
        # accept an unrelated 200 response (e.g. an error envelope).
        if not any(
            key in payload for key in ("main_deck", "extra_deck", "side_deck")
        ):
            continue
        if cache_path:
            with open(cache_path, "w") as f:
                json.dump(payload, f, sort_keys=True)
        return payload, False

    tried_summary = ", ".join(f"{u}={s}" for u, s in tried)
    message = (
        f"YGOPRODeck detail: no candidate URL yielded a usable payload for "
        f"deck_id={deck_id} (tried: {tried_summary})"
    )
    if last_error is not None:
        raise requests.RequestException(message) from last_error
    raise requests.RequestException(message)


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


def verify_deck_sample(list_row, detail_payload, *, observed_at, cache_hit=False, now=None, detail_source="detail"):
    """Produce a sanitized per-deck verification row with explicit
    PASS / MISSING / FAIL states for every required field. Never fabricates
    tournament dates, placements, player counts, or publication timestamps.

    ``now`` sets the reference for relative-timestamp decoding (e.g.
    ``3 days ago``). Defaults to ``_now_utc()``.

    ``detail_source`` records where the deck-array data came from: one of
    ``"detail"`` (the JSON detail endpoint returned it), ``"catalogue"``
    (the catalogue row's own ``main_deck``/``extra_deck``/``side_deck``
    fields were used because the detail endpoint was unavailable), or
    ``"none"`` (neither source had the data). It is stamped verbatim onto
    each of ``main_deck_source``/``extra_deck_source``/``side_deck_source``
    so downstream summaries can report catalogue-only vs detail-API
    completeness separately."""
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
    # Iterate every documented/observed date-field variant on both list row
    # and detail payload; the first non-empty raw value is authoritative.
    raw_event_date = None
    event_date_key_used = None
    for source in (list_row, detail):
        raw, key = _extract_event_date_raw(source)
        if raw:
            raw_event_date = raw
            event_date_key_used = key
            break
    parsed_event_date = _parse_event_date(raw_event_date, now=now) if raw_event_date else None
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

    # Per-array provenance for the catalogue-vs-detail split summary. If the
    # array is absent everywhere, the source is "none"; otherwise it's
    # whatever the caller declared (the fetcher/sampler knows).
    def _source_for(arr):
        if arr is None:
            return "none"
        return detail_source

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
        "main_deck_source": _source_for(main_arr),
        "extra_deck_present_status": extra_present,
        "extra_deck_nonempty_status": extra_nonempty,
        "extra_deck_count": len(extra_arr) if extra_arr is not None else None,
        "extra_deck_source": _source_for(extra_arr),
        "side_deck_present_status": side_present,
        "side_deck_nonempty_status": side_nonempty,
        "side_deck_count": len(side_arr) if side_arr is not None else None,
        "side_deck_source": _source_for(side_arr),
        "structure_complete_status": structure_complete,
        "structure_complete_source": (
            detail_source
            if structure_complete == "PASS"
            else "none"
        ),
        "event_date_key_used": event_date_key_used,
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
    """Return (selected_rows, exclusion_reason_counts). Never returns rows that
    lack a deck_id or that belong to a non-TCG_ADVANCED population. Rows with
    an unparseable/missing event date are still eligible for sampling — they
    just sort last, and their FAIL/MISSING state will be recorded honestly by
    the verifier. This guarantees that "the source has no dates we can parse"
    is surfaced as a per-record FAIL, not as a silent zero-sample."""
    reasons = defaultdict(int)
    candidates = []
    for row in rows:
        if not _is_paper_tcg_record(row):
            reasons["excluded_by_keyword_or_wrong_format"] += 1
            continue
        population = _classify_population(row)
        if population != POPULATION_TCG_ADVANCED:
            reasons["non_tcg_advanced_population"] += 1
            continue
        deck_id = _extract_deck_id(row)
        if deck_id is None:
            reasons["no_deck_id"] += 1
            continue
        parsed, _raw, _key, parse_ok = _extract_event_date(row)
        if not parse_ok:
            # Recorded as an exclusion *reason* but not as an exclusion: the row
            # still moves forward so the verifier can label the date FAIL.
            reasons["date_missing_or_unparseable_kept_for_sampling"] += 1
            sort_key = datetime.min.replace(tzinfo=timezone.utc)
        else:
            sort_key = parsed
        candidates.append((sort_key, deck_id, row))
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    seen = set()
    selected = []
    for _sort_key, deck_id, row in candidates:
        if deck_id in seen:
            reasons["duplicate_deck_id"] += 1
            continue
        seen.add(deck_id)
        selected.append(row)
        if len(selected) >= sample_size:
            break
    return selected, dict(reasons)


def _date_key_present(record, key):
    """A date-candidate key counts as present if it holds a non-empty
    string OR any numeric (epoch) value. Zero-length string is absent."""
    value = record.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return False


def build_list_response_diagnostics(rows, *, sample_keys_from=5, now=None):
    """Produce a sanitized diagnostic block describing the list response
    shape. Emits KEY NAMES only — never values, player identifiers, or full
    payloads. Safe to attach to the CI artifact.

    Fields:
      raw_row_count                    -- total rows returned by getDecks.php
      keys_observed_sample             -- sorted union of top-level keys from
                                          the first `sample_keys_from` rows
      deck_id_key_present_count        -- how many rows expose ANY deck-id key
      deck_id_keys_seen                -- which of DECK_ID_KEYS appear in data
      event_date_key_present_count     -- rows with ANY date-candidate key
      event_date_keys_seen             -- which of EVENT_DATE_KEYS appear
      event_date_parseable_count       -- rows whose date-candidate parses
      passes_is_paper_tcg_record       -- rows surviving the paper-TCG filter
      classified_tcg_advanced          -- of those, in TCG_ADVANCED bucket
      population_bucket_counts         -- disjoint bucket histogram
      status                           -- OK | INCONCLUSIVE
    """
    raw_row_count = len(rows)
    keys_union = set()
    for row in rows[:sample_keys_from]:
        if isinstance(row, dict):
            keys_union.update(row.keys())

    deck_id_key_present = 0
    deck_id_keys_seen = set()
    event_date_key_present = 0
    event_date_keys_seen = set()
    event_date_parseable = 0
    event_date_shape_counts = defaultdict(int)
    passes_paper = 0
    passes_tcg_advanced = 0
    population_counts = defaultdict(int)

    for row in rows:
        if not isinstance(row, dict):
            continue
        # Deck-id key visibility.
        row_deck_id_keys = [key for key in DECK_ID_KEYS if key in row and row.get(key) not in (None, "")]
        if row_deck_id_keys:
            deck_id_key_present += 1
            deck_id_keys_seen.update(row_deck_id_keys)
        # Event-date key visibility.
        row_date_keys = [key for key in EVENT_DATE_KEYS if _date_key_present(row, key)]
        if row_date_keys:
            event_date_key_present += 1
            event_date_keys_seen.update(row_date_keys)
        raw_event_value, _key = _extract_event_date_raw(row)
        if raw_event_value is not None:
            event_date_shape_counts[_classify_event_date_shape(raw_event_value)] += 1
        _parsed, _raw, _key, parse_ok = _extract_event_date(row, now=now)
        if parse_ok:
            event_date_parseable += 1
        if _is_paper_tcg_record(row):
            passes_paper += 1
            bucket = _classify_population(row)
            population_counts[bucket] += 1
            if bucket == POPULATION_TCG_ADVANCED:
                passes_tcg_advanced += 1

    status = "OK" if passes_tcg_advanced > 0 and deck_id_key_present > 0 else "INCONCLUSIVE"

    return {
        "endpoint": YGOPRODECK_API_URL,
        "raw_row_count": raw_row_count,
        "keys_observed_sample": sorted(keys_union),
        "keys_observed_sample_source_rows": min(sample_keys_from, raw_row_count),
        "deck_id_key_present_count": deck_id_key_present,
        "deck_id_keys_seen": sorted(deck_id_keys_seen),
        "deck_id_keys_probed": list(DECK_ID_KEYS),
        "event_date_key_present_count": event_date_key_present,
        "event_date_keys_seen": sorted(event_date_keys_seen),
        "event_date_keys_probed": list(EVENT_DATE_KEYS),
        "event_date_parseable_count": event_date_parseable,
        "event_date_shape_counts": dict(event_date_shape_counts),
        "passes_is_paper_tcg_record": passes_paper,
        "classified_tcg_advanced": passes_tcg_advanced,
        "population_bucket_counts": dict(population_counts),
        "status": status,
    }


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
    selected, exclusion_reasons = _select_recent_paper_rows(rows, sample_size)

    last_call_ref = {"t": None}
    http_status_counter = defaultdict(lambda: defaultdict(int))
    details = []
    errors = []
    detail_success_count = 0
    detail_failure_count = 0
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
                _http_status_counter=http_status_counter,
            )
        except (requests.RequestException, ValueError) as exc:
            errors.append({"deck_id": deck_id, "error": str(exc)})
            detail_failure_count += 1
            # Detail HTTP failed. Fall back on card arrays the *catalogue*
            # row already carries (comma-separated strings or JSON arrays).
            # This is not substitution from an unrelated field — it's the
            # same field the source already delivered. Provenance is
            # explicitly recorded as `catalogue`.
            details.append(
                verify_deck_sample(
                    row,
                    _catalogue_row_as_detail(row),
                    observed_at=observed_at,
                    cache_hit=False,
                    now=now,
                    detail_source="catalogue",
                )
            )
            continue
        detail_success_count += 1
        details.append(
            verify_deck_sample(
                row,
                detail_payload,
                observed_at=observed_at,
                cache_hit=cache_hit,
                now=now,
                detail_source="detail",
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

    # Split coverage summaries: (a) catalogue-only, i.e. what getDecks.php
    # ALONE delivered; (b) detail-API, marked unavailable when every
    # candidate URL 404s. These are always both emitted so a reader can
    # judge the source without inferring provenance from field_totals.
    catalogue_only_coverage = _summarize_catalogue_only(details)
    detail_api_coverage = _summarize_detail_api(
        details,
        http_status_counter,
        detail_success_count=detail_success_count,
        detail_failure_count=detail_failure_count,
    )

    return {
        "sample_size_target": sample_size,
        "sample_size_actual": len(details),
        "sample_kind": "top_cut_only",
        "endpoint_candidates": [url for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES],
        "endpoint": YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0],
        "detail_probe_summary": {
            url: {str(status): count for status, count in statuses.items()}
            for url, statuses in http_status_counter.items()
        },
        "min_interval_seconds": float(min_interval_seconds),
        "observed_at": observed_at,
        "population_bucket_counts": dict(populations),
        "field_totals": {field: dict(counts) for field, counts in totals.items()},
        "decks": details,
        "detail_errors": errors,
        "exclusion_reason_counts": exclusion_reasons,
        "catalogue_only_coverage": catalogue_only_coverage,
        "detail_api_coverage": detail_api_coverage,
    }


_METADATA_STATUS_FIELDS_CATALOGUE = (
    "tournament_name_status",
    "event_date_status",
    "player_name_status",
    "placement_status",
    "player_count_status",
)


def _summarize_catalogue_only(details):
    """Report completeness attributable to the catalogue (`getDecks.php`)
    endpoint ALONE. Every record in ``details`` is inspected: metadata is
    ALWAYS catalogue-sourced (list-row-first extraction is verified), and
    the deck-array subtotals count only rows whose ``*_source`` marker is
    ``catalogue``. Provenance is stamped verbatim so this block is safe
    to interpret as "what getDecks.php delivered on its own"."""
    sample_size = len(details)
    metadata_totals = {
        field: {"PASS": 0, "MISSING": 0, "FAIL": 0}
        for field in _METADATA_STATUS_FIELDS_CATALOGUE
    }
    metadata_complete = 0
    structure_complete_from_catalogue = 0
    main_from_catalogue = 0
    extra_from_catalogue = 0
    side_from_catalogue = 0
    for record in details:
        record_metadata_pass = True
        for field in _METADATA_STATUS_FIELDS_CATALOGUE:
            state = record.get(field, "MISSING")
            if state not in ("PASS", "MISSING", "FAIL"):
                state = "MISSING"
            metadata_totals[field][state] += 1
            if state != "PASS":
                record_metadata_pass = False
        if record_metadata_pass:
            metadata_complete += 1
        if record.get("main_deck_source") == "catalogue":
            main_from_catalogue += 1
        if record.get("extra_deck_source") == "catalogue":
            extra_from_catalogue += 1
        if record.get("side_deck_source") == "catalogue":
            side_from_catalogue += 1
        if (
            record.get("structure_complete_status") == "PASS"
            and record.get("structure_complete_source") == "catalogue"
        ):
            structure_complete_from_catalogue += 1
    return {
        "provenance": "getDecks.php (catalogue)",
        "sample_size": sample_size,
        "metadata_field_totals": metadata_totals,
        "metadata_complete_count": metadata_complete,
        "structure_complete_from_catalogue": structure_complete_from_catalogue,
        "main_deck_from_catalogue": main_from_catalogue,
        "extra_deck_from_catalogue": extra_from_catalogue,
        "side_deck_from_catalogue": side_from_catalogue,
    }


def _summarize_detail_api(
    details,
    http_status_counter,
    *,
    detail_success_count,
    detail_failure_count,
):
    """Report detail-endpoint availability and, when available, its
    contribution to structure-complete. When EVERY candidate URL returned
    only non-2xx (or every attempt errored), status is marked
    ``"unavailable"`` and structure counts sourced from detail are zero
    by definition."""
    any_2xx = any(
        any(str(code).startswith("2") for code in statuses)
        for statuses in http_status_counter.values()
    )
    structure_complete_from_detail = sum(
        1
        for record in details
        if record.get("structure_complete_status") == "PASS"
        and record.get("structure_complete_source") == "detail"
    )
    status = "available" if any_2xx else "unavailable"
    return {
        "provenance": "get.php / getDeck.php / deck.php (JSON detail endpoints)",
        "candidate_urls": [url for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES],
        "http_status_summary": {
            url: {str(status): count for status, count in statuses.items()}
            for url, statuses in http_status_counter.items()
        },
        "detail_success_count": detail_success_count,
        "detail_failure_count": detail_failure_count,
        "structure_complete_from_detail": structure_complete_from_detail,
        "status": status,
    }


def _catalogue_row_as_detail(row):
    """Extract a minimal detail-shaped dict from a catalogue row when the
    detail endpoint is unavailable. Only carries fields the catalogue
    already sent — never invents. Empty/unusable strings pass through
    unchanged so downstream classification still treats them as MISSING."""
    if not isinstance(row, dict):
        return {}
    passthrough = {}
    for key in ("main_deck", "extra_deck", "side_deck"):
        if key in row:
            passthrough[key] = row[key]
    return passthrough


def run_experiment(
    output_path,
    lookback_days=90,
    timeout=30,
    sample_size=TOP_CUT_SAMPLE_SIZE,
    cache_dir=None,
    min_interval_seconds=DETAIL_MIN_INTERVAL_SECONDS,
    session=None,
    repeatability_runs=1,
):
    from_date = (_now_utc() - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    client = session or requests.Session()

    # Repeatability: fetch the catalogue N times and record deckNum-set
    # stability. N=1 skips the extra fetches. Every extra fetch is paced
    # by the same rate-limit floor and can be seeded by cache when the
    # cache_dir points at a shared location.
    repeatability_runs = max(1, int(repeatability_runs))
    catalogue_runs = []
    for _ in range(repeatability_runs):
        run_rows = fetch_tcg_decks(from_date=from_date, timeout=timeout, session=client)
        # Deterministic pacing between repeat calls.
        if min_interval_seconds > 0 and repeatability_runs > 1:
            time.sleep(min_interval_seconds)
        catalogue_runs.append(run_rows)
    # The first run's rows drive the actual coverage evaluation.
    rows = catalogue_runs[0]
    diagnostics = build_list_response_diagnostics(rows)
    report = build_coverage_report(rows)
    report["list_response_diagnostics"] = diagnostics
    sample_block = sample_top_cut_details(
        rows,
        sample_size=sample_size,
        session=client,
        cache_dir=cache_dir,
        timeout=timeout,
        min_interval_seconds=min_interval_seconds,
    )
    report["top_cut_sample"] = sample_block
    # Promote the two coverage sub-blocks to top level so consumers do not
    # need to reach into `top_cut_sample` to distinguish catalogue vs
    # detail-API results.
    catalogue_coverage = dict(sample_block["catalogue_only_coverage"])
    detail_api_coverage = dict(sample_block["detail_api_coverage"])
    report["catalogue_coverage"] = catalogue_coverage
    report["detail_api_coverage"] = detail_api_coverage

    # Repeatability block: how stable are the catalogue's deckNum sets
    # across repeated fetches? A `sufficient_for_decklist` verdict is
    # only meaningful if the catalogue itself is deterministic run-to-run.
    report["repeatability"] = _build_repeatability_report(catalogue_runs)

    # Sufficiency verdict: is getDecks.php ALONE enough to satisfy this
    # project's decklist + metadata requirements?
    verdict = _decide_catalogue_sufficiency(
        catalogue_coverage,
        detail_api_coverage,
        sample_size=sample_block["sample_size_actual"],
    )
    report["catalogue_sufficient_for_decklist"] = verdict["sufficient"]
    report["recommendation"] = verdict["recommendation"]
    report["decision_rationale"] = verdict["rationale"]

    # Overall status: INCONCLUSIVE if the sample is zero, OR if both
    # sources fail to yield any complete decklist. Otherwise OK — even if
    # only the catalogue succeeded, because that is a legitimate result
    # the split summary makes explicit.
    structure_pass = (
        sample_block.get("field_totals", {})
        .get("structure_complete_status", {})
        .get("PASS", 0)
    )
    if sample_block["sample_size_actual"] == 0:
        report["overall_status"] = "INCONCLUSIVE"
    elif structure_pass == 0 and detail_api_coverage["status"] == "unavailable":
        report["overall_status"] = "INCONCLUSIVE"
    else:
        report["overall_status"] = "OK"
    write_report(output_path, report)
    return report


_CATALOGUE_METADATA_SUFFICIENCY_THRESHOLD = 0.90
_CATALOGUE_STRUCTURE_SUFFICIENCY_THRESHOLD = 0.90


def _decide_catalogue_sufficiency(catalogue_coverage, detail_api_coverage, *, sample_size):
    """Return {sufficient, recommendation, rationale} evaluating whether
    getDecks.php ALONE is sufficient for the project's decklist +
    metadata requirements.

    Definition of "sufficient": ≥90% of the sampled decks have complete
    Main/Extra/Side sourced from the catalogue AND ≥90% have complete
    tournament metadata (tournament name, event date, player name,
    placement, player count). If either threshold fails, or the sample
    is empty, the answer is False.

    A recommendation string is included so the artifact tells a human
    reader exactly what to do next (repeatability check vs. rejection)."""
    if sample_size <= 0:
        return {
            "sufficient": False,
            "recommendation": "INCONCLUSIVE",
            "rationale": (
                "Sample size is zero; cannot evaluate sufficiency. "
                "Re-run after fixing catalogue selection."
            ),
        }
    structure_from_catalogue = catalogue_coverage["structure_complete_from_catalogue"]
    metadata_complete = catalogue_coverage["metadata_complete_count"]
    structure_ratio = structure_from_catalogue / sample_size
    metadata_ratio = metadata_complete / sample_size
    structure_ok = structure_ratio >= _CATALOGUE_STRUCTURE_SUFFICIENCY_THRESHOLD
    metadata_ok = metadata_ratio >= _CATALOGUE_METADATA_SUFFICIENCY_THRESHOLD

    if structure_ok and metadata_ok:
        return {
            "sufficient": True,
            "recommendation": (
                "USE_CATALOGUE_ONLY: getDecks.php alone satisfies decklist "
                "and metadata requirements. Confirm with a repeatability "
                "check across multiple workflow runs before promoting."
            ),
            "rationale": (
                f"structure_complete_from_catalogue={structure_from_catalogue}/{sample_size} "
                f"({structure_ratio:.0%}); metadata_complete_count={metadata_complete}/{sample_size} "
                f"({metadata_ratio:.0%}); detail_api_status={detail_api_coverage['status']}."
            ),
        }
    return {
        "sufficient": False,
        "recommendation": (
            "REJECT_FOR_DECKLIST_PIPELINE: getDecks.php alone is below the "
            "sufficiency thresholds and the detail API is not usable. Do "
            "not add more guessed URLs; look at an alternate source."
        ),
        "rationale": (
            f"structure_complete_from_catalogue={structure_from_catalogue}/{sample_size} "
            f"({structure_ratio:.0%}, need >= {_CATALOGUE_STRUCTURE_SUFFICIENCY_THRESHOLD:.0%}); "
            f"metadata_complete_count={metadata_complete}/{sample_size} "
            f"({metadata_ratio:.0%}, need >= {_CATALOGUE_METADATA_SUFFICIENCY_THRESHOLD:.0%}); "
            f"detail_api_status={detail_api_coverage['status']}."
        ),
    }


def _build_repeatability_report(catalogue_runs):
    """Compare the catalogue's top-N deckNum set across repeated fetches.
    Returns a sanitized dict with run count, per-run row counts, and set
    intersection metrics. Never surfaces card names or player data."""
    run_count = len(catalogue_runs)
    per_run_row_counts = [len(rows) for rows in catalogue_runs]
    per_run_deck_id_sets = []
    for rows in catalogue_runs:
        ids = set()
        for row in rows:
            deck_id = _extract_deck_id(row) if isinstance(row, dict) else None
            if deck_id is not None:
                ids.add(deck_id)
        per_run_deck_id_sets.append(ids)
    if per_run_deck_id_sets:
        intersection = set(per_run_deck_id_sets[0])
        union = set(per_run_deck_id_sets[0])
        for s in per_run_deck_id_sets[1:]:
            intersection &= s
            union |= s
    else:
        intersection = set()
        union = set()
    stable_ratio = (len(intersection) / len(union)) if union else 0.0
    verdict = "STABLE" if run_count > 1 and stable_ratio >= 0.95 else (
        "UNSTABLE" if run_count > 1 else "NOT_TESTED"
    )
    return {
        "run_count": run_count,
        "per_run_row_counts": per_run_row_counts,
        "deck_id_set_intersection_size": len(intersection),
        "deck_id_set_union_size": len(union),
        "deck_id_set_stability_ratio": stable_ratio,
        "verdict": verdict,
    }


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
    parser.add_argument(
        "--repeatability-runs",
        type=int,
        default=1,
        help=(
            "Number of catalogue fetches to perform (default 1). When >1, "
            "the report includes a `repeatability` block comparing deckNum "
            "sets across runs."
        ),
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
            repeatability_runs=args.repeatability_runs,
        )
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"YGOPRODeck coverage check failed: {exc}") from exc

    sample_block = report.get("top_cut_sample", {}) or {}
    diagnostics = report.get("list_response_diagnostics", {}) or {}
    overall_status = report.get("overall_status", "INCONCLUSIVE")
    summary = {
        "output": args.output,
        "overall_status": overall_status,
        "catalogue_sufficient_for_decklist": report.get(
            "catalogue_sufficient_for_decklist"
        ),
        "recommendation": report.get("recommendation"),
        "decision_rationale": report.get("decision_rationale"),
        "catalogue_coverage": report.get("catalogue_coverage"),
        "detail_api_coverage": report.get("detail_api_coverage"),
        "repeatability": report.get("repeatability"),
        "events_found": report["events_found"],
        "last_14_days_events": report["coverage_windows"]["last_14_days"]["events"],
        "last_30_days_events": report["coverage_windows"]["last_30_days"]["events"],
        "last_90_days_events": report["coverage_windows"]["last_90_days"]["events"],
        "top_cut_sample_size_actual": sample_block.get("sample_size_actual", 0),
        "top_cut_structure_complete_totals": sample_block.get("field_totals", {}).get(
            "structure_complete_status", {}
        ),
        "top_cut_population_bucket_counts": sample_block.get("population_bucket_counts", {}),
        "top_cut_exclusion_reason_counts": sample_block.get("exclusion_reason_counts", {}),
        "list_response_diagnostics": {
            "raw_row_count": diagnostics.get("raw_row_count"),
            "keys_observed_sample": diagnostics.get("keys_observed_sample"),
            "deck_id_keys_seen": diagnostics.get("deck_id_keys_seen"),
            "event_date_keys_seen": diagnostics.get("event_date_keys_seen"),
            "event_date_parseable_count": diagnostics.get("event_date_parseable_count"),
            "classified_tcg_advanced": diagnostics.get("classified_tcg_advanced"),
            "status": diagnostics.get("status"),
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if overall_status != "OK":
        # Zero-candidate / shape-mismatch runs must fail the workflow so
        # nobody predeclares success from a green tick.
        raise SystemExit(
            f"YGOPRODeck coverage check inconclusive: overall_status={overall_status}"
        )


if __name__ == "__main__":
    main()
