"""
Collect newly published TCG Advanced tournament decklists from YGOPRODeck
and import them into the Meta Watch dataset as a secondary/backfill source.

Design constraints (see META_WATCH.md and the PR #8 audit):

- Only the ``https://ygoprodeck.com/api/decks/getDecks.php`` catalogue
  endpoint is used. The unavailable detail API endpoints are never called,
  and HTML pages on ygoprodeck.com are never scraped.
- Only TCG Advanced (``_sft_category=Tournament Meta Decks``) records are
  considered. OCG, Master Duel, Rush Duel, Genesys, online/remote/casual
  markers, etc. are excluded here -- they must never leak in as
  ``TCG_ADVANCED`` observations.
- All HTTP requests use HTTPS with certificate verification enabled and a
  conservative pacing delay between paginated calls.
- Records are deduplicated by YGOPRODeck's stable ``deckNum`` field before
  importing, both against the existing dataset (via the collection report)
  and within the same batch.
- Missing metadata (e.g. no ``tournamentPlayerName``, no parseable
  ``submit_date``) is reported honestly as a rejected record; the collector
  never guesses or backfills a value it does not have.
- On a catalogue endpoint failure the existing dataset is left completely
  untouched and the failure is reported.

This collector is a secondary/backfill source: it appends new observations
alongside whatever the Konami collector has already imported, and never
removes or rewrites any pre-existing entry.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, ".")

from app.meta_watch import (  # noqa: E402
    FORMAT_TCG_ADVANCED,
    dedupe_key,
    load_dataset,
    validate_observation,
)
from scripts.import_meta_watch_lists import import_observations_payload  # noqa: E402
from scripts.ygoprodeck_card_catalogue import (  # noqa: E402
    CatalogueError,
    load_passcode_map,
    resolve_observation_cards,
)

DEFAULT_CACHE_DIR = "data/cache/ygoprodeck"

YGOPRODECK_API_URL = "https://ygoprodeck.com/api/decks/getDecks.php"
TCG_CATEGORY = "Tournament Meta Decks"
DEFAULT_DATASET_PATH = "data/meta_watch_lists.json"
DEFAULT_REPORT_PATH = "data/meta_watch_ygoprodeck_report.json"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_PAGE_SIZE = 20
DEFAULT_REQUEST_PACING_SECONDS = 1.0
DEFAULT_TIMEOUT = 30
SOURCE_NAME = "YGOPRODeck Tournament Meta Decks (TCG)"

# Non-TCG-Advanced markers -- any of these appearing in the record's
# format/deck name/tournament name/description/excerpt disqualifies the row.
# Keep in sync with scripts/check_ygoprodeck_tcg_coverage.py so filtering
# stays consistent with the pre-integration audit.
_NON_TCG_ADVANCED_KEYWORDS = (
    "ocg",
    "master duel",
    "duel links",
    "genesys",
    "speed duel",
    "rush duel",
    "online",
    "remote",
    "duelingbook",
    "edo pro",
    "edopro",
    "test",
    "practice",
    "casual",
)

_URL_SCHEME_RE = re.compile(r"^https://", re.IGNORECASE)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_RELATIVE_DATE_RE = re.compile(
    r"^\s*(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\s*$",
    re.IGNORECASE,
)
_JUST_NOW_RE = re.compile(r"^\s*just\s+now\s*$", re.IGNORECASE)
_TODAY_RE = re.compile(r"^\s*today\s*$", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"^\s*yesterday\s*$", re.IGNORECASE)

# Approximate day counts for coarse relative units. Months/years do not have
# a fixed length; using 30/365 gives an honest best-effort event_date while
# forcing quality="date_only" so no invented sub-day precision leaks out.
_RELATIVE_UNIT_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_str(value):
    return value.strip() if isinstance(value, str) else ""


def _slugify(text):
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    return slug or "unknown"


def _parse_deck_array(raw):
    """
    YGOPRODeck deck zones are returned either as a JSON array
    (``[89631139, 89631139, ...]``) or as a comma-separated string
    (``"89631139,89631139,..."``). Both encode a card id per entry, with
    duplicates representing the copy count. Return an ordered list of
    string ids, preserving unrecognised values as their string form so we
    never silently drop cards.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [piece.strip() for piece in text.split(",") if piece.strip()]


def _cards_from_ids(card_ids):
    """Aggregate a list of card ids into ``{name, count}`` entries.

    We deliberately keep the YGOPRODeck card id as the ``name`` (as a
    string) so downstream consumers can resolve to a display name via the
    prices/cards database rather than us guessing.
    """
    counts = {}
    order = []
    for card_id in card_ids:
        if card_id not in counts:
            order.append(card_id)
        counts[card_id] = counts.get(card_id, 0) + 1
    return [{"name": name, "count": counts[name]} for name in order]


def _parse_absolute_date(raw):
    """Parse an absolute date string into a ``YYYY-MM-DD`` string, or None."""
    text = _safe_str(raw)
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_absolute_timestamp(raw):
    """Parse a submit_date into an ISO8601 UTC timestamp, or None."""
    text = _safe_str(raw)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _iso(parsed.astimezone(timezone.utc))


def _resolve_relative_submit_date(text, now):
    """
    Resolve a relative ``submit_date`` string against the collector's UTC
    run time (``now``).

    Accepted forms (case-insensitive, extra whitespace tolerated):

    - ``"just now"`` -> sub-day precision, anchored at ``now``.
    - ``"N minutes ago"`` / ``"N hours ago"`` -> sub-day precision,
      ``now`` minus the delta.
    - ``"today"`` -> today's UTC date, no sub-day precision.
    - ``"yesterday"`` -> yesterday's UTC date, no sub-day precision.
    - ``"N days/weeks/months/years ago"`` -> ``now`` minus an approximate
      day-count delta (30/365 for months/years), no sub-day precision.

    Returns ``(event_date, published_at, quality)`` where ``quality`` is
    ``"timestamp"`` when we have sub-day precision, ``"date_only"`` when
    we only have the calendar day, or ``None`` when ``text`` is not a
    supported relative form (caller falls back to absolute parsing).

    Vague expressions such as ``"a few days ago"`` or ``"recently"`` are
    intentionally not recognised here -- they fall through and are
    reported as ``unparseable`` upstream.
    """
    if _JUST_NOW_RE.match(text):
        return now.strftime("%Y-%m-%d"), _iso(now), "timestamp"
    if _TODAY_RE.match(text):
        return now.strftime("%Y-%m-%d"), None, "date_only"
    if _YESTERDAY_RE.match(text):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d"), None, "date_only"
    match = _RELATIVE_DATE_RE.match(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "minute":
        anchored = now - timedelta(minutes=amount)
        return anchored.strftime("%Y-%m-%d"), _iso(anchored), "timestamp"
    if unit == "hour":
        anchored = now - timedelta(hours=amount)
        return anchored.strftime("%Y-%m-%d"), _iso(anchored), "timestamp"
    days = _RELATIVE_UNIT_DAYS[unit] * amount
    anchored = now - timedelta(days=days)
    return anchored.strftime("%Y-%m-%d"), None, "date_only"


def _classify_submit_date(raw, now):
    """
    Return ``(event_date, published_at, quality)`` for a submit_date value.

    Recognised relative expressions (``"3 days ago"``, ``"today"``,
    ``"yesterday"``, ``"just now"``, ``"N minutes/hours/days/weeks/months/
    years ago"``) are anchored to the collector's UTC run time ``now`` so
    the row can be imported. Sub-day precision (``just now``, minutes,
    hours) produces a timestamp; coarser expressions produce a
    ``date_only`` event_date with no ``published_at`` (we refuse to
    invent a clock time we do not have).

    Absolute values (``YYYY-MM-DD``, ISO8601 timestamps) behave as
    before. Vague or unrecognised strings are reported as
    ``unparseable``; empty values as ``missing``. We never fall back to
    ``updated`` / ``edit_date`` here -- that is a caller-side decision
    and is deliberately not done anywhere in this collector.
    """
    text = _safe_str(raw)
    if not text:
        return None, None, "missing"
    resolved = _resolve_relative_submit_date(text, now)
    if resolved is not None:
        return resolved
    event_date = _parse_absolute_date(text)
    if event_date is None:
        return None, None, "unparseable"
    published_at = _parse_absolute_timestamp(text)
    if published_at is None:
        return event_date, None, "date_only"
    return event_date, published_at, "timestamp"


def _is_tcg_advanced_record(record):
    """
    True only when the row is unambiguously TCG Advanced. Any hint of OCG,
    Master Duel, Rush, Genesys, online/remote/casual/test/practice markers
    disqualifies the row -- these must never be imported as TCG_ADVANCED.
    """
    format_name = _safe_str(record.get("format")).lower()
    if format_name and TCG_CATEGORY.lower() not in format_name:
        return False
    blob = " ".join(
        _safe_str(record.get(field))
        for field in (
            "format",
            "deck_name",
            "tournamentName",
            "deck_description",
            "deck_excerpt",
        )
    ).lower()
    return not any(token in blob for token in _NON_TCG_ADVANCED_KEYWORDS)


def _pretty_url_to_source(record):
    pretty = _safe_str(record.get("pretty_url"))
    if not pretty:
        return None
    # pretty_url is a slug; construct the canonical https deck permalink.
    url = f"https://ygoprodeck.com/deck/{pretty.lstrip('/')}"
    return url if _URL_SCHEME_RE.match(url) else None


def _stable_deck_num(record):
    value = record.get("deckNum")
    if isinstance(value, int):
        return str(value)
    text = _safe_str(value)
    return text or None


def _banlist_id_from_date(event_date):
    return f"UNKNOWN-{event_date[:7]}" if event_date else None


def record_to_observation(record, now=None):
    """
    Convert one ``getDecks.php`` record into a Meta Watch observation, or
    return ``(None, reason)`` if the record cannot be honestly represented.
    """
    now = now or _now_utc()

    deck_num = _stable_deck_num(record)
    if not deck_num:
        return None, "missing deckNum stable id"

    if not _is_tcg_advanced_record(record):
        return None, "not TCG Advanced"

    event_date, published_at, quality = _classify_submit_date(record.get("submit_date"), now)
    if event_date is None:
        return None, f"submit_date not usable ({quality})"

    tournament_name = _safe_str(record.get("tournamentName"))
    if not tournament_name:
        return None, "missing tournamentName"

    source_url = _pretty_url_to_source(record)
    if not source_url:
        return None, "missing pretty_url"

    player_name = _safe_str(record.get("tournamentPlayerName"))
    if not player_name:
        return None, "missing tournamentPlayerName"

    main_ids = _parse_deck_array(record.get("main_deck"))
    extra_ids = _parse_deck_array(record.get("extra_deck"))
    side_ids = _parse_deck_array(record.get("side_deck"))
    if not (main_ids or extra_ids or side_ids):
        return None, "empty main/extra/side deck arrays"

    archetype = _safe_str(record.get("deck_name")) or "UNKNOWN"
    placement_raw = record.get("tournamentPlacement")
    if isinstance(placement_raw, int):
        placement = str(placement_raw)
    else:
        placement = _safe_str(placement_raw) or None

    player_count = record.get("tournamentPlayerCount")
    if not isinstance(player_count, int):
        player_count = None

    event_slug = f"ygoprodeck-{_slugify(tournament_name)}-{event_date}"

    observation = {
        "event_id": event_slug,
        "event_name": tournament_name,
        "event_date": event_date,
        "region": "UNKNOWN",
        "format": FORMAT_TCG_ADVANCED,
        "banlist_id": _banlist_id_from_date(event_date),
        "player": player_name,
        "placement": placement,
        "archetype": archetype,
        "source_url": source_url,
        "source_type": "tournament",
        "published_at": published_at,
        "first_seen_at": _iso(now),
        "main_deck": _cards_from_ids(main_ids),
        "side_deck": _cards_from_ids(side_ids),
        "extra_deck": _cards_from_ids(extra_ids),
        # Provenance -- kept as extra fields for reporting/audit and stable
        # dedup; validate_observation ignores unknown keys.
        "source_provider": "ygoprodeck",
        "source_deck_id": deck_num,
        "tournament_player_count": player_count,
        "submit_date_quality": quality,
    }
    return observation, None


def _cache_key(base_url, params):
    stable_params = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return json.dumps({"url": base_url, "params": stable_params}, sort_keys=True)


def fetch_tcg_decks(
    from_date,
    timeout=DEFAULT_TIMEOUT,
    session=None,
    page_size=DEFAULT_PAGE_SIZE,
    pacing_seconds=DEFAULT_REQUEST_PACING_SECONDS,
    sleep=time.sleep,
    cache=None,
):
    """
    Page through YGOPRODeck's TCG Advanced feed with conservative pacing.

    - HTTPS-only endpoint; ``requests`` performs certificate verification by
      default (``verify=True``) and is never overridden here.
    - A configurable ``pacing_seconds`` delay is applied between page
      requests to avoid hammering the endpoint.
    - An optional in-memory ``cache`` dict keyed by URL+params lets repeat
      runs (or retries within a run) skip identical HTTP calls.
    """
    client = session or requests.Session()
    cache = cache if cache is not None else {}
    offset = 0
    all_rows = []
    first_call = True
    while True:
        params = {
            "_sft_category": TCG_CATEGORY,
            "sort": "Updated",
            "from": from_date,
            "limit": page_size,
            "offset": offset,
        }
        key = _cache_key(YGOPRODECK_API_URL, params)
        if key in cache:
            page = cache[key]
        else:
            if not first_call and pacing_seconds > 0:
                sleep(pacing_seconds)
            response = client.get(
                YGOPRODECK_API_URL,
                params=params,
                timeout=timeout,
                verify=True,
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise ValueError("YGOPRODeck getDecks.php returned non-list JSON")
            cache[key] = page
        first_call = False
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_rows


def _write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _extra_provenance_fields(obs):
    return {
        "source_provider": obs.get("source_provider"),
        "source_deck_id": obs.get("source_deck_id"),
        "tournament_player_count": obs.get("tournament_player_count"),
        "submit_date_quality": obs.get("submit_date_quality"),
    }


def collect_and_import(
    dataset_path=DEFAULT_DATASET_PATH,
    report_path=DEFAULT_REPORT_PATH,
    dry_run=False,
    lookback_days=DEFAULT_LOOKBACK_DAYS,
    timeout=DEFAULT_TIMEOUT,
    session=None,
    pacing_seconds=DEFAULT_REQUEST_PACING_SECONDS,
    sleep=time.sleep,
    now=None,
    cache_dir=DEFAULT_CACHE_DIR,
    passcode_map=None,
    catalogue_session=None,
):
    """
    Fetch the current TCG Advanced feed from YGOPRODeck, filter/normalise
    rows into Meta Watch observations, resolve YGOPRODeck deck-array
    passcodes to canonical card names via the YGOPRODeck cardinfo
    catalogue (fetched at most once per run and cached locally), and
    import the resolved observations.

    On a catalogue endpoint failure -- either the decklist feed or the
    cardinfo catalogue -- the dataset is left untouched and the failure
    is reported in the returned report dict.

    ``passcode_map`` may be supplied by callers/tests to skip the
    catalogue fetch entirely (useful for offline determinism).
    """
    now = now or _now_utc()
    from_date = (now - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")

    existing = load_dataset(dataset_path)
    existing_obs = existing.get("observations", [])
    existing_deck_ids = {
        _safe_str(o.get("source_deck_id"))
        for o in existing_obs
        if o.get("source_provider") == "ygoprodeck" and _safe_str(o.get("source_deck_id"))
    }
    existing_by_key = {dedupe_key(o): o for o in existing_obs}

    report = {
        "collected_at": _iso(now),
        "source": SOURCE_NAME,
        "endpoint": YGOPRODECK_API_URL,
        "dataset_path": dataset_path,
        "dry_run": dry_run,
        "lookback_days": lookback_days,
        "from_date": from_date,
        "endpoint_failure": None,
        "records_fetched": 0,
        "records_excluded_non_tcg_advanced": 0,
        "candidate_observations": 0,
        "duplicate_existing_precheck_skipped": 0,
        "duplicate_in_batch_precheck_skipped": 0,
        "rejected_records": [],
        "unresolved_passcode_records": [],
        "card_catalogue": {"source": None, "size": 0},
        "import": None,
    }

    try:
        rows = fetch_tcg_decks(
            from_date=from_date,
            timeout=timeout,
            session=session,
            pacing_seconds=pacing_seconds,
            sleep=sleep,
        )
    except (requests.RequestException, ValueError) as exc:
        report["endpoint_failure"] = {
            "endpoint": YGOPRODECK_API_URL,
            "error": str(exc),
        }
        report["import"] = {
            "input_path": None,
            "dataset_path": dataset_path,
            "dry_run": dry_run,
            "added": 0,
            "duplicate_existing_skipped": 0,
            "duplicate_in_batch_skipped": 0,
            "rejected": [],
        }
        _write_json(report_path, report)
        return report

    report["records_fetched"] = len(rows)

    # Fetch the card catalogue exactly once per run (or reuse the local
    # cache). Failure here leaves the dataset untouched and is reported
    # honestly -- we refuse to import passcodes as card names.
    if passcode_map is None:
        try:
            passcode_map, catalogue_source = load_passcode_map(
                cache_dir=cache_dir,
                session=catalogue_session,
                timeout=timeout,
                sleep=sleep,
            )
        except CatalogueError as exc:
            report["endpoint_failure"] = {
                "endpoint": "ygoprodeck cardinfo",
                "error": str(exc),
            }
            report["import"] = {
                "input_path": None,
                "dataset_path": dataset_path,
                "dry_run": dry_run,
                "added": 0,
                "duplicate_existing_skipped": 0,
                "duplicate_in_batch_skipped": 0,
                "rejected": [],
            }
            _write_json(report_path, report)
            return report
    else:
        catalogue_source = "provided"
    report["card_catalogue"] = {"source": catalogue_source, "size": len(passcode_map)}

    seen_batch_deck_ids = set()
    seen_batch_dedupe_keys = set()
    candidates = []

    for row in rows:
        deck_num = _stable_deck_num(row)
        # Fast, honest exclusion counter for the audit report: we count
        # anything filtered by TCG Advanced classification separately from
        # rows rejected for missing metadata.
        if not _is_tcg_advanced_record(row):
            report["records_excluded_non_tcg_advanced"] += 1
            continue

        observation, reason = record_to_observation(row, now=now)
        if observation is None:
            report["rejected_records"].append(
                {"deckNum": deck_num, "pretty_url": _safe_str(row.get("pretty_url")), "reason": reason}
            )
            continue

        # Resolve YGOPRODeck deck-array passcodes to canonical card
        # names. If any passcode cannot be resolved we refuse the row
        # entirely rather than silently producing a partial deck -- the
        # unresolved passcodes/counts are reported honestly.
        resolved_obs, unresolved_by_zone = resolve_observation_cards(
            observation, passcode_map
        )
        if resolved_obs is None:
            report["unresolved_passcode_records"].append(
                {
                    "deckNum": deck_num,
                    "pretty_url": _safe_str(row.get("pretty_url")),
                    "unresolved": unresolved_by_zone,
                }
            )
            continue
        observation = resolved_obs

        errors = validate_observation(observation)
        if errors:
            report["rejected_records"].append(
                {
                    "deckNum": deck_num,
                    "pretty_url": _safe_str(row.get("pretty_url")),
                    "reason": "; ".join(errors),
                }
            )
            continue

        deck_id = observation["source_deck_id"]
        if deck_id in existing_deck_ids:
            report["duplicate_existing_precheck_skipped"] += 1
            continue
        if deck_id in seen_batch_deck_ids:
            report["duplicate_in_batch_precheck_skipped"] += 1
            continue

        key = dedupe_key(observation)
        if key in existing_by_key:
            report["duplicate_existing_precheck_skipped"] += 1
            continue
        if key in seen_batch_dedupe_keys:
            report["duplicate_in_batch_precheck_skipped"] += 1
            continue

        seen_batch_deck_ids.add(deck_id)
        seen_batch_dedupe_keys.add(key)
        candidates.append(observation)

    report["candidate_observations"] = len(candidates)

    import_result = import_observations_payload(
        {"observations": candidates}, dataset_path=dataset_path, dry_run=dry_run
    )
    report["import"] = import_result
    report["imported_provenance_sample"] = [
        _extra_provenance_fields(obs) for obs in candidates[:5]
    ]
    _write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Collect TCG Advanced tournament decklists from YGOPRODeck's "
            "getDecks.php catalogue endpoint and import them into the "
            "Meta Watch dataset as a secondary/backfill source."
        )
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH, help="Path to data/meta_watch_lists.json")
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH, help="Where to write the collection/import report")
    parser.add_argument("--dry-run", action="store_true", help="Collect/validate/report without writing dataset changes")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Fetch window in days")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds per request")
    parser.add_argument(
        "--pacing-seconds",
        type=float,
        default=DEFAULT_REQUEST_PACING_SECONDS,
        help="Delay between paginated catalogue requests, in seconds",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help="Local directory for caching the YGOPRODeck cardinfo catalogue",
    )
    args = parser.parse_args()

    result = collect_and_import(
        dataset_path=args.dataset,
        report_path=args.report,
        dry_run=args.dry_run,
        lookback_days=args.lookback_days,
        timeout=args.timeout,
        pacing_seconds=args.pacing_seconds,
        cache_dir=args.cache_dir,
    )
    print(json.dumps(result, indent=2))
    if result.get("endpoint_failure"):
        sys.exit(1)


if __name__ == "__main__":
    main()
