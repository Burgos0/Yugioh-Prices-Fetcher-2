"""
Read-only TopDeck coverage check for Yu-Gi-Oh Advanced tournaments.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import requests

TOPDECK_API_BASE_URL = "https://topdeck.gg/api"
TARGET_GAME = "Yu-Gi-Oh"
TARGET_FORMAT = "Advanced"
WINDOW_DAYS = (14, 30, 90)
_DIGITAL_HINTS = ("master duel", "duel links", "md ", "rush duel")


def _utc_now():
    return datetime.now(timezone.utc)


def _to_unix_seconds(dt):
    return int(dt.timestamp())


def _from_unix_seconds(value):
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(dt):
    return dt.strftime("%Y-%m-%d")


def _safe_event_date(event):
    start = event.get("startDate")
    if start is None:
        return None
    try:
        return _from_unix_seconds(start)
    except (TypeError, ValueError, OSError):
        return None


def _parse_explicit_publication_timestamp(event):
    for key in ("publishedAt", "publicationDate", "published_at", "publication_date"):
        value = event.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            try:
                return _iso(_from_unix_seconds(value))
            except (TypeError, ValueError, OSError):
                continue
        if isinstance(value, str) and value.strip():
            candidate = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _iso(parsed.astimezone(timezone.utc))
    return None


def _structured_zone_presence(deck_obj):
    if not isinstance(deck_obj, dict):
        return {"main": False, "side": False, "extra": False}
    presence = {"main": False, "side": False, "extra": False}
    for key, value in deck_obj.items():
        if not value:
            continue
        key_lower = str(key).lower()
        if "main" in key_lower:
            presence["main"] = True
        elif "side" in key_lower:
            presence["side"] = True
        elif "extra" in key_lower:
            presence["extra"] = True
    return presence


def classify_player_deck_source(player):
    zones = _structured_zone_presence(player.get("deckObj"))
    if all(zones.values()):
        return "complete_structured"
    if player.get("decklist"):
        return "external_link"
    return "missing_or_incomplete"


def _initial_player_coverage():
    return {"complete_structured": 0, "external_link": 0, "missing_or_incomplete": 0}


def _rollup_events(events, now):
    coverage = {}
    for days in WINDOW_DAYS:
        cutoff = now - timedelta(days=days)
        filtered = [e for e in events if e.get("_event_dt") and e["_event_dt"] > cutoff]
        players = _initial_player_coverage()
        for event in filtered:
            for key in players:
                players[key] += event["players"][key]
        coverage[f"last_{days}_days"] = {
            "window_days": days,
            "events": len(filtered),
            "players": players,
        }
    return coverage


def _paper_distinction_assessment(events):
    non_matching = []
    digital_name_matches = []
    for event in events:
        if event.get("game") != TARGET_GAME or event.get("format") != TARGET_FORMAT:
            non_matching.append(
                {"tid": event.get("tid"), "game": event.get("game"), "format": event.get("format")}
            )
        name = (event.get("event_name") or "").lower()
        if any(hint in name for hint in _DIGITAL_HINTS):
            digital_name_matches.append({"tid": event.get("tid"), "event_name": event.get("event_name")})
    if non_matching:
        return {
            "reliably_distinguished": False,
            "assessment": "not_reliable",
            "reason": "API response included events outside Yu-Gi-Oh Advanced filter.",
            "non_matching_events": non_matching,
            "digital_name_matches": digital_name_matches,
        }
    if digital_name_matches:
        return {
            "reliably_distinguished": False,
            "assessment": "uncertain",
            "reason": "All events match filter fields, but some names include digital-population indicators.",
            "non_matching_events": [],
            "digital_name_matches": digital_name_matches,
        }
    return {
        "reliably_distinguished": True,
        "assessment": "likely_reliable",
        "reason": "All returned events match Yu-Gi-Oh Advanced and no digital-population name indicators were found.",
        "non_matching_events": [],
        "digital_name_matches": [],
    }


def summarize_tournament(event):
    event_dt = _safe_event_date(event)
    standings = event.get("standings") if isinstance(event.get("standings"), list) else None
    player_coverage = _initial_player_coverage()
    players = standings or []
    for player in players:
        player_coverage[classify_player_deck_source(player)] += 1

    return {
        "tid": event.get("TID"),
        "event_name": event.get("tournamentName"),
        "game": event.get("game"),
        "format": event.get("format"),
        "event_date": _date(event_dt) if event_dt else None,
        "participant_count": len(players) if standings is not None else None,
        "publication_timestamp": _parse_explicit_publication_timestamp(event),
        "players": player_coverage,
        "_event_dt": event_dt,
    }


def build_coverage_report(tournaments, lookback_days=90, now=None):
    now = now or _utc_now()
    events = [summarize_tournament(event) for event in tournaments]
    events.sort(key=lambda e: e["_event_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    overall_players = _initial_player_coverage()
    for event in events:
        for key in overall_players:
            overall_players[key] += event["players"][key]
    report = {
        "generated_at": _iso(now),
        "query": {
            "game": TARGET_GAME,
            "format": TARGET_FORMAT,
            "lookback_days": lookback_days,
        },
        "events_found": len(events),
        "events": [
            {k: v for k, v in event.items() if k != "_event_dt"}
            for event in events
        ],
        "overall_players": overall_players,
        "paper_tcg_advanced_distinction": _paper_distinction_assessment(events),
        "coverage_windows": _rollup_events(events, now),
    }
    return report


def fetch_tournaments(api_key, lookback_days=90, timeout=30, api_base_url=TOPDECK_API_BASE_URL, session=None):
    payload = {
        "game": TARGET_GAME,
        "format": TARGET_FORMAT,
        "last": int(lookback_days),
        "players": ["name", "id", "decklist"],
        "columns": ["name", "decklist", "id"],
    }
    client = session or requests.Session()
    response = client.post(
        f"{api_base_url}/v2/tournaments",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("TopDeck tournaments endpoint returned non-list JSON")
    return data


def write_report(path, report):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def run_coverage_check(output_path, api_key, lookback_days=90, timeout=30, api_base_url=TOPDECK_API_BASE_URL):
    tournaments = fetch_tournaments(
        api_key=api_key,
        lookback_days=lookback_days,
        timeout=timeout,
        api_base_url=api_base_url,
    )
    report = build_coverage_report(tournaments=tournaments, lookback_days=lookback_days)
    write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Run read-only TopDeck coverage check for Yu-Gi-Oh Advanced.")
    parser.add_argument("--output", required=True, help="Path to write sanitized coverage report JSON")
    parser.add_argument("--lookback-days", type=int, default=90, help="Tournament lookback window in days")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--api-base-url", default=TOPDECK_API_BASE_URL, help="TopDeck API base URL")
    args = parser.parse_args()

    api_key = os.getenv("TOPDECK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("TOPDECK_API_KEY is required")

    try:
        report = run_coverage_check(
            output_path=args.output,
            api_key=api_key,
            lookback_days=args.lookback_days,
            timeout=args.timeout,
            api_base_url=args.api_base_url,
        )
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"TopDeck coverage check failed: {exc}") from exc
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
