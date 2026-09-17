"""
Meta Watch: research-only "published-list adoption" tracking for tournament
decklist tech choices, compared against relatively flat tracked prices.

This module never fetches decklists itself. It only reads a locally stored,
versioned JSON dataset of previously imported observations (see
scripts/import_meta_watch_lists.py) and the existing read-only prices.db for
price context. It must never make a live network request during page loads.

IMPORTANT: these are "published-list adoption" statistics -- the share of
imported, deduplicated tournament decklists that contain a card -- NOT an
estimate of overall metagame share, and never a price prediction or a
purchase recommendation.

Dataset file format (see scripts/import_meta_watch_lists.py for the importer
and validation rules):
{
  "schema_version": 1,
  "observations": [
    {
      "event_id": str, "event_name": str, "event_date": "YYYY-MM-DD",
      "region": str, "format": "TCG_ADVANCED" | "OCG" | "MASTER_DUEL" | "RUSH_DUEL" | "OTHER",
      "banlist_id": str, "player": str, "placement": str|int|null,
      "archetype": str, "source_url": str, "source_type": "tournament"|"casual",
      "published_at": "YYYY-MM-DDTHH:MM:SSZ"|null,
      "first_seen_at": "YYYY-MM-DDTHH:MM:SSZ",
      "archived_at": "YYYY-MM-DDTHH:MM:SSZ",
      "main_deck": [{"name": str, "count": int}], "side_deck": [...], "extra_deck": [...]
    }, ...
  ],
  "revisions": [ ...full corrected observations... ]
}
"""
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

DEFAULT_DATASET_PATH = "data/meta_watch_lists.json"
SCHEMA_VERSION = 1

FORMAT_TCG_ADVANCED = "TCG_ADVANCED"
FORMAT_OCG = "OCG"
FORMAT_MASTER_DUEL = "MASTER_DUEL"
FORMAT_RUSH_DUEL = "RUSH_DUEL"
FORMAT_OTHER = "OTHER"
VALID_FORMATS = (FORMAT_TCG_ADVANCED, FORMAT_OCG, FORMAT_MASTER_DUEL, FORMAT_RUSH_DUEL, FORMAT_OTHER)

SOURCE_TOURNAMENT = "tournament"
SOURCE_CASUAL = "casual"
VALID_SOURCE_TYPES = (SOURCE_TOURNAMENT, SOURCE_CASUAL)

ZONES = ("main_deck", "side_deck", "extra_deck")
ZONE_PREFIX = {"main_deck": "main", "side_deck": "side", "extra_deck": "extra"}

REQUIRED_FIELDS = ("event_id", "event_name", "event_date", "region", "format",
                    "banlist_id", "player", "archetype", "source_url", "source_type")

# Provisional, configurable thresholds -- see analysis functions below.
DEFAULT_WINDOW_DAYS = 14
DEFAULT_MIN_LISTS = 20
DEFAULT_MIN_EVENTS = 3
DEFAULT_FLAT_PCT_THRESHOLD = 5.0


def validate_observation(obs):
    """
    Validate one raw decklist observation dict. Returns a list of human
    -readable error strings (empty list means valid). Never guesses or
    fills in a missing required field -- an invalid observation must be
    rejected by the importer, not silently patched.
    """
    errors = []
    if not isinstance(obs, dict):
        return ["observation is not a JSON object"]

    for field in REQUIRED_FIELDS:
        value = obs.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required field: {field}")

    fmt = obs.get("format")
    if fmt is not None and fmt not in VALID_FORMATS:
        errors.append(f"invalid format: {fmt!r} (must be one of {VALID_FORMATS})")

    source_type = obs.get("source_type")
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        errors.append(f"invalid source_type: {source_type!r} (must be one of {VALID_SOURCE_TYPES})")

    event_date = obs.get("event_date")
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            errors.append(f"invalid event_date: {event_date!r} (expected YYYY-MM-DD)")

    for ts_field in ("published_at", "first_seen_at", "archived_at"):
        ts = obs.get(ts_field)
        if ts:
            try:
                _parse_timestamp(ts)
            except ValueError:
                errors.append(f"invalid {ts_field}: {ts!r} (expected ISO8601 UTC, e.g. 2026-09-01T00:00:00Z)")

    has_any_zone = False
    for zone in ZONES:
        entries = obs.get(zone)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"{zone} must be a list")
            continue
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry or "count" not in entry:
                errors.append(f"{zone} entries must be objects with 'name' and 'count'")
                continue
            if not isinstance(entry["name"], str) or not entry["name"].strip():
                errors.append(f"{zone} entry has invalid name: {entry.get('name')!r}")
            if not isinstance(entry["count"], int) or entry["count"] <= 0:
                errors.append(f"{zone} entry has invalid count: {entry.get('count')!r}")
            has_any_zone = True
    if not has_any_zone:
        errors.append("observation has no card entries in main_deck/side_deck/extra_deck")

    return errors


def _parse_timestamp(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def dedupe_key(obs):
    """Same player's list from the same event is one observation, regardless of casing/whitespace."""
    return (str(obs.get("event_id", "")).strip().lower(), str(obs.get("player", "")).strip().lower())


def revision_key(obs):
    """Prefer a provider's stable deck id, falling back to event/player identity."""
    provider = str(obs.get("source_provider", "")).strip().lower()
    deck_id = str(obs.get("source_deck_id", "")).strip()
    if provider and deck_id:
        return ("source", provider, deck_id)
    return ("event_player",) + dedupe_key(obs)


def dedupe_observations(observations):
    """Keep the first occurrence per stable revision identity; report dropped rows."""
    seen = set()
    deduped = []
    duplicate_count = 0
    for obs in observations:
        key = revision_key(obs)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        deduped.append(obs)
    return deduped, duplicate_count


def load_dataset(path=DEFAULT_DATASET_PATH):
    """Load the versioned dataset. Returns an empty, valid skeleton if the file doesn't exist yet."""
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "observations": []}
    with open(path) as f:
        data = json.load(f)
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("observations", [])
    return data


def save_dataset(dataset, path=DEFAULT_DATASET_PATH):
    """Persist the dataset, preserving raw imported observations verbatim."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(dataset, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def get_zone_entries(obs, zone):
    """Return [(name, count), ...] for one zone, combining any repeated names."""
    combined = defaultdict(int)
    for entry in obs.get(zone) or []:
        combined[entry["name"]] += entry["count"]
    return list(combined.items())


def get_cutoff_datetime(obs):
    """Publication-time cutoff: use published_at when known, else first_seen_at."""
    ts = obs.get("published_at") or obs.get("first_seen_at")
    if not ts:
        return None
    return _parse_timestamp(ts)


def select_archived_observations(dataset, as_of=None):
    """Select the latest archived version per deck, optionally at a UTC-day cutoff.

    Historical selection trusts only importer-owned ``archived_at`` values.
    Missing or invalid archive timestamps are excluded and counted. After
    version selection, observations with an invalid or future ``event_date``
    are also excluded and counted.
    """
    originals = list(dataset.get("observations") or [])
    revisions = list(dataset.get("revisions") or [])
    if as_of is None:
        latest = {revision_key(obs): obs for obs in originals}
        for revision in revisions:
            key = revision_key(revision)
            if key in latest:
                latest[key] = revision
        return list(latest.values()), {
            "unknown_archive_timestamp_excluded": 0,
            "invalid_event_date_excluded": 0,
            "future_event_date_excluded": 0,
        }

    try:
        cutoff_day = datetime.strptime(as_of, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)")
    cutoff = datetime.combine(cutoff_day, time.max, tzinfo=timezone.utc)

    latest = {}
    latest_archived = {}
    unknown_archive = 0
    for obs in originals + revisions:
        archived_at = obs.get("archived_at")
        try:
            archived = _parse_timestamp(archived_at).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            unknown_archive += 1
            continue
        if archived > cutoff:
            continue
        key = revision_key(obs)
        if key not in latest_archived or archived >= latest_archived[key]:
            latest[key] = obs
            latest_archived[key] = archived

    selected = []
    invalid_event_date = 0
    future_event_date = 0
    for obs in latest.values():
        try:
            event_date = datetime.strptime(obs.get("event_date"), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            invalid_event_date += 1
            continue
        if event_date > cutoff_day:
            future_event_date += 1
            continue
        selected.append(obs)
    return selected, {
        "unknown_archive_timestamp_excluded": unknown_archive,
        "invalid_event_date_excluded": invalid_event_date,
        "future_event_date_excluded": future_event_date,
    }


def filter_tournament(observations):
    return [o for o in observations if o.get("source_type") == SOURCE_TOURNAMENT]


def group_by_banlist(observations):
    groups = defaultdict(list)
    for obs in observations:
        groups[obs.get("banlist_id")].append(obs)
    return groups


def _window_stats(observations):
    """Aggregate one time window's lists into per-card and per-archetype counters."""
    total_lists = len(observations)
    total_events = {o["event_id"] for o in observations}
    archetype_totals = Counter()
    archetype_events = defaultdict(set)
    cards = {}

    for obs in observations:
        archetype = obs.get("archetype")
        archetype_totals[archetype] += 1
        archetype_events[archetype].add(obs["event_id"])

        zones_by_card = defaultdict(dict)
        for zone in ZONES:
            for name, count in get_zone_entries(obs, zone):
                zones_by_card[name][zone] = count

        for name, zones in zones_by_card.items():
            card = cards.setdefault(name, {
                "lists": 0, "events": set(), "total_copies_sum": 0,
                "main_lists": 0, "side_lists": 0, "extra_lists": 0,
                "main_copies": 0, "side_copies": 0, "extra_copies": 0,
                "by_archetype": defaultdict(lambda: {"lists": 0, "events": set()}),
            })
            card["lists"] += 1
            card["events"].add(obs["event_id"])
            card["total_copies_sum"] += sum(zones.values())
            for zone in ZONES:
                if zone in zones:
                    prefix = ZONE_PREFIX[zone]
                    card[f"{prefix}_lists"] += 1
                    card[f"{prefix}_copies"] += zones[zone]
            ab = card["by_archetype"][archetype]
            ab["lists"] += 1
            ab["events"].add(obs["event_id"])

    return {
        "total_lists": total_lists,
        "total_events": total_events,
        "archetype_totals": archetype_totals,
        "archetype_events": archetype_events,
        "cards": cards,
    }


def _archetype_breakdown(name, recent_stats, prior_stats):
    archetypes = set()
    recent_card = recent_stats["cards"].get(name)
    prior_card = prior_stats["cards"].get(name)
    if recent_card:
        archetypes.update(recent_card["by_archetype"].keys())
    if prior_card:
        archetypes.update(prior_card["by_archetype"].keys())

    breakdown = []
    for archetype in sorted(archetypes):
        recent_total = recent_stats["archetype_totals"].get(archetype, 0)
        prior_total = prior_stats["archetype_totals"].get(archetype, 0)
        recent_lists = recent_card["by_archetype"].get(archetype, {}).get("lists", 0) if recent_card else 0
        prior_lists = prior_card["by_archetype"].get(archetype, {}).get("lists", 0) if prior_card else 0
        recent_pct = (recent_lists / recent_total * 100) if recent_total else None
        prior_pct = (prior_lists / prior_total * 100) if prior_total else None
        breakdown.append({
            "archetype": archetype,
            "recent_lists": recent_lists, "recent_total_lists": recent_total,
            "prior_lists": prior_lists, "prior_total_lists": prior_total,
            "recent_adoption_pct": recent_pct, "prior_adoption_pct": prior_pct,
            "pct_point_change": (recent_pct - prior_pct) if (recent_pct is not None and prior_pct is not None) else None,
        })
    return breakdown


def compute_adoption(recent_observations, prior_observations, min_lists=DEFAULT_MIN_LISTS,
                      min_events=DEFAULT_MIN_EVENTS):
    """
    Compare a recent window of tournament lists to the preceding window.

    Returns a dict with `insufficient_data` (bool), `reason` (str or None),
    window summaries, a ranked `cards` list (established-baseline cards
    sorted by adoption percentage-point increase, event support as
    tie-breaker), and a separate `new_cards` list (zero appearances in the
    prior window -- kept separate so growth is never reported as infinite).
    """
    recent_stats = _window_stats(recent_observations)
    prior_stats = _window_stats(prior_observations)

    reasons = []
    if recent_stats["total_lists"] < min_lists or len(recent_stats["total_events"]) < min_events:
        reasons.append("recent window has too few published lists or events")
    if prior_stats["total_lists"] < min_lists or len(prior_stats["total_events"]) < min_events:
        reasons.append("prior window has too few published lists or events")

    result = {
        "insufficient_data": bool(reasons),
        "reason": "; ".join(reasons) if reasons else None,
        "min_lists": min_lists,
        "min_events": min_events,
        "recent_total_lists": recent_stats["total_lists"],
        "recent_total_events": len(recent_stats["total_events"]),
        "prior_total_lists": prior_stats["total_lists"],
        "prior_total_events": len(prior_stats["total_events"]),
        "cards": [],
        "new_cards": [],
    }
    if reasons:
        return result

    all_names = set(recent_stats["cards"]) | set(prior_stats["cards"])
    qualifying, new_cards = [], []
    for name in all_names:
        recent_card = recent_stats["cards"].get(name)
        prior_card = prior_stats["cards"].get(name)
        recent_lists = recent_card["lists"] if recent_card else 0
        prior_lists = prior_card["lists"] if prior_card else 0
        recent_pct = recent_lists / recent_stats["total_lists"] * 100
        prior_pct = prior_lists / prior_stats["total_lists"] * 100

        entry = {
            "card_name": name,
            "recent_lists": recent_lists,
            "recent_total_lists": recent_stats["total_lists"],
            "prior_lists": prior_lists,
            "prior_total_lists": prior_stats["total_lists"],
            "recent_adoption_pct": recent_pct,
            "prior_adoption_pct": prior_pct,
            "pct_point_change": recent_pct - prior_pct,
            "avg_copies": (recent_card["total_copies_sum"] / recent_card["lists"]) if recent_card and recent_card["lists"] else None,
            "distinct_events": len(recent_card["events"]) if recent_card else 0,
            "main_lists": recent_card["main_lists"] if recent_card else 0,
            "side_lists": recent_card["side_lists"] if recent_card else 0,
            "extra_lists": recent_card["extra_lists"] if recent_card else 0,
            "archetype_breakdown": _archetype_breakdown(name, recent_stats, prior_stats),
        }

        if prior_lists == 0 and recent_lists > 0:
            new_cards.append(entry)
        elif recent_lists > 0:
            qualifying.append(entry)

    qualifying.sort(key=lambda e: (-e["pct_point_change"], -e["distinct_events"], e["card_name"]))
    new_cards.sort(key=lambda e: (-e["distinct_events"], -e["recent_lists"], e["card_name"]))

    result["cards"] = qualifying
    result["new_cards"] = new_cards
    return result


def resolve_card_printings(conn, card_name):
    """
    Find every price-tracked printing whose base card name matches this
    decklist card name exactly, or as "<name> (<printing detail>)". Returns
    [] (unresolved) rather than ever guessing a close match.
    """
    like_pattern = card_name.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + " (%"
    rows = conn.execute(
        "SELECT DISTINCT product_id, card_name, set_name FROM prices "
        "WHERE card_name = ? OR card_name LIKE ? ESCAPE '\\'",
        (card_name, like_pattern),
    ).fetchall()
    return [{"product_id": r[0], "card_name": r[1], "set_name": r[2]} for r in rows]


# Dash variants seen across sources (e.g. an official article using an en
# dash where prices.db's tracked printing uses a plain hyphen) for the same
# real card. Normalizing these is punctuation cleanup, not fuzzy/guessed
# identity matching -- it never merges two differently-named cards.
_DASH_CHARS = ("\u2013", "\u2014", "\u2212")


def _normalize_dashes(name):
    for ch in _DASH_CHARS:
        name = name.replace(ch, "-")
    return name


def build_normalized_name_index(conn):
    """One-time (per report) index of every distinct tracked printing, keyed by its dash-normalized card_name."""
    index = defaultdict(list)
    for product_id, card_name, set_name in conn.execute("SELECT DISTINCT product_id, card_name, set_name FROM prices"):
        index[_normalize_dashes(card_name)].append(
            {"product_id": product_id, "card_name": card_name, "set_name": set_name})
    return index


def resolve_card_printings_with_fallback(conn, card_name, normalized_index):
    """
    Try the exact/prefix match first; only if that resolves nothing, retry
    with dash punctuation normalized on both sides. A card name whose
    normalized form is merely a prefix of another distinct card's normalized
    name still never merges, since the fallback applies the same "<name> ("
    boundary rule as resolve_card_printings.
    """
    printings = resolve_card_printings(conn, card_name)
    if printings:
        return printings
    normalized = _normalize_dashes(card_name)
    exact = normalized_index.get(normalized)
    if exact:
        return exact
    prefix_key = normalized + " ("
    matches = []
    for key, rows in normalized_index.items():
        if key.startswith(prefix_key):
            matches.extend(rows)
    return matches


def pick_primary_printing(conn, printings):
    """
    Deterministically pick one printing per card for price display: the one
    with the most verified price history rows, tie-broken by lowest
    product_id. Never chosen by price direction/performance.
    """
    if not printings:
        return None
    counts = {}
    for p in printings:
        counts[p["product_id"]] = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE product_id = ? AND market_price IS NOT NULL",
            (p["product_id"],),
        ).fetchone()[0]
    return sorted(printings, key=lambda p: (-counts[p["product_id"]], p["product_id"]))[0]["product_id"]


def get_price_context(conn, product_id, flat_pct_threshold=DEFAULT_FLAT_PCT_THRESHOLD):
    """
    Latest verified price, freshness (days behind the DB's latest known
    date), and an experimental 7-day change/"flat" flag. Never infers
    scarcity/volume/pull rates; missing history is reported explicitly
    rather than guessed.
    """
    global_max = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    row = conn.execute(
        "SELECT date, market_price FROM prices WHERE product_id = ? AND market_price IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if row is None:
        return {"has_price": False, "flat_pct_threshold": flat_pct_threshold}

    latest_date, latest_price = row
    freshness_days = None
    if global_max:
        freshness_days = (datetime.strptime(global_max, "%Y-%m-%d") - datetime.strptime(latest_date, "%Y-%m-%d")).days

    anchor = datetime.strptime(latest_date, "%Y-%m-%d")
    target = (anchor - timedelta(days=7)).strftime("%Y-%m-%d")
    lower_bound = (anchor - timedelta(days=10)).strftime("%Y-%m-%d")
    prior_row = conn.execute(
        "SELECT market_price FROM prices WHERE product_id = ? AND market_price IS NOT NULL "
        "AND date <= ? AND date >= ? ORDER BY date DESC LIMIT 1",
        (product_id, target, lower_bound),
    ).fetchone()

    change_7d_pct = None
    is_flat = None
    if prior_row and prior_row[0]:
        change_7d_pct = (latest_price - prior_row[0]) / prior_row[0] * 100
        is_flat = abs(change_7d_pct) <= flat_pct_threshold

    return {
        "has_price": True,
        "latest_date": latest_date,
        "latest_price": latest_price,
        "freshness_days": freshness_days,
        "change_7d_pct": change_7d_pct,
        "is_flat": is_flat,
        "verified_7d_history": prior_row is not None,
        "flat_pct_threshold": flat_pct_threshold,
    }


def attach_price_context(entries, prices_db_path):
    """
    Resolve each ranked card entry's identity/printings and attach price
    context in place. If prices_db_path doesn't exist (e.g. a fresh
    checkout before the snapshot restore step has run -- prices.db/
    signals.db are gitignored, not committed), every entry is marked
    unresolved with an explicit "price_db_unavailable" flag rather than
    raising, so a missing local database can never crash report
    generation or the page.
    """
    if not entries:
        return
    if not os.path.exists(prices_db_path):
        for entry in entries:
            entry.update({"resolved": False, "printings": [], "price_db_unavailable": True})
        return
    conn = sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True)
    try:
        normalized_index = None
        cache = {}
        for entry in entries:
            name = entry["card_name"]
            if name not in cache:
                printings = resolve_card_printings_with_fallback(
                    conn, name, normalized_index if normalized_index is not None else {})
                if not printings and normalized_index is None:
                    # Build the (larger, dash-normalized) fallback index lazily,
                    # only once, and only the first time it's actually needed.
                    normalized_index = build_normalized_name_index(conn)
                    printings = resolve_card_printings_with_fallback(conn, name, normalized_index)
                if not printings:
                    cache[name] = {"resolved": False, "printings": []}
                else:
                    primary = pick_primary_printing(conn, printings)
                    cache[name] = {
                        "resolved": True,
                        "printings": printings,
                        "primary_product_id": primary,
                        "price": get_price_context(conn, primary),
                    }
            entry.update(cache[name])
    finally:
        conn.close()


def build_meta_watch_report(dataset_path=DEFAULT_DATASET_PATH, prices_db_path="data/prices.db",
                             as_of=None, window_days=DEFAULT_WINDOW_DAYS, min_lists=DEFAULT_MIN_LISTS,
                             min_events=DEFAULT_MIN_EVENTS, flat_pct_threshold=DEFAULT_FLAT_PCT_THRESHOLD,
                             target_format=FORMAT_TCG_ADVANCED):
    """
    Build the full Meta Watch report for one tournament format (default: TCG
    Advanced). Formats/banlists are never combined silently -- results are
    grouped per banlist_id, each evaluated against its own two 14-day
    windows.
    """
    dataset = load_dataset(dataset_path)
    all_observations = dataset.get("observations", [])
    tournament_only = filter_tournament(all_observations)
    casual_excluded = len(all_observations) - len(tournament_only)

    deduped, duplicate_count = dedupe_observations(tournament_only)
    format_population_counts = Counter(o.get("format") for o in deduped)
    format_filtered = [o for o in deduped if o.get("format") == target_format]
    other_format_excluded = len(deduped) - len(format_filtered)
    non_target_format_counts = {
        fmt: count for fmt, count in sorted(format_population_counts.items()) if fmt != target_format
    }

    report = {
        "target_format": target_format,
        "dataset_path": dataset_path,
        "price_db_available": os.path.exists(prices_db_path),
        "window_days": window_days,
        "min_lists": min_lists,
        "min_events": min_events,
        "flat_pct_threshold": flat_pct_threshold,
        "total_raw_observations": len(all_observations),
        "casual_excluded": casual_excluded,
        "duplicate_observations_skipped": duplicate_count,
        "other_format_excluded": other_format_excluded,
        "format_population_counts": dict(sorted(format_population_counts.items())),
        "non_target_format_counts": non_target_format_counts,
        "banlists": [],
    }

    if not format_filtered:
        return report

    dated = []
    for obs in format_filtered:
        cutoff = get_cutoff_datetime(obs)
        if cutoff is not None:
            dated.append((cutoff, obs))

    if as_of is None:
        if not dated:
            return report
        as_of = max(cutoff for cutoff, _ in dated)
    elif isinstance(as_of, str):
        as_of = _parse_timestamp(as_of)

    recent_start = as_of - timedelta(days=window_days)
    prior_start = as_of - timedelta(days=2 * window_days)

    groups = group_by_banlist(format_filtered)
    for banlist_id in sorted(groups, key=lambda b: (b is None, b)):
        observations = groups[banlist_id]
        recent, prior = [], []
        for obs in observations:
            cutoff = get_cutoff_datetime(obs)
            if cutoff is None:
                continue
            if recent_start < cutoff <= as_of:
                recent.append(obs)
            elif prior_start < cutoff <= recent_start:
                prior.append(obs)

        adoption = compute_adoption(recent, prior, min_lists=min_lists, min_events=min_events)
        if not adoption["insufficient_data"]:
            attach_price_context(adoption["cards"], prices_db_path)
            attach_price_context(adoption["new_cards"], prices_db_path)

        report["banlists"].append({
            "banlist_id": banlist_id,
            "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recent_window_start": recent_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "prior_window_start": prior_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **adoption,
        })

    return report
