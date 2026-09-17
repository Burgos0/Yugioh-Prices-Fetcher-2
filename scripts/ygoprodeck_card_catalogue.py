"""
Card-identity bridge for Meta Watch: fetch the official YGOPRODeck card
catalogue once per run and build an exact passcode -> canonical card-name
map.

Design constraints (matches the Meta Watch project's honest-reporting
policy called out in META_WATCH.md and the existing YGOPRODeck coverage
audit):

- One HTTPS call to ``https://db.ygoprodeck.com/api/v7/cardinfo.php`` per
  run at most, not one per card. ``requests`` verifies TLS certificates by
  default (``verify=True``); the flag is never overridden.
- A conservative timeout and small retry loop with pacing between
  attempts. Retries are only attempted on connection/timeout/5xx-style
  ``requests`` errors -- HTTP 4xx and JSON schema errors fail immediately.
- A local JSON cache under a caller-supplied cache directory. If the
  cache is present it is reused verbatim so repeated runs and backfills
  don't hit the API again.
- The passcode map is built exclusively from the API's ``id`` (int) and
  ``name`` (string) fields. Nothing else is invented.
- Numeric ``name`` strings (like ``"14558128"``) are the passcode form
  written by ``scripts.collect_ygoprodeck_lists``; they are resolved
  case-insensitively? No -- they are pure digits, so the lookup is exact.
  Non-numeric ``name`` strings (already canonical) are preserved as-is.

This module never writes into ``data/meta_watch_lists.json`` directly and
never chooses a specific printing / product_id -- printing selection is
explicitly out of scope for the identity bridge.
"""
import json
import os
import time

import requests

YGOPRODECK_CARDINFO_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
DEFAULT_CACHE_FILENAME = "ygoprodeck_cardinfo.json"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_PACING_SECONDS = 1.0


class CatalogueError(RuntimeError):
    """Raised when the YGOPRODeck catalogue cannot be obtained honestly."""


def _is_numeric_passcode(value):
    """A YGOPRODeck deck-array passcode is a non-empty string of digits."""
    return isinstance(value, str) and value.isdigit() and len(value) > 0


def build_passcode_map(cards):
    """
    Build a ``{passcode(str) -> canonical_name(str)}`` map from the API's
    ``data`` array. Only entries with an integer ``id`` and a non-empty
    ``name`` are included. Later duplicates for the same passcode do not
    overwrite earlier ones (the API returns each card once anyway; this
    is a defensive belt-and-braces).
    """
    mapping = {}
    if not isinstance(cards, list):
        return mapping
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_id = card.get("id")
        name = card.get("name")
        if not isinstance(card_id, int):
            continue
        if not isinstance(name, str):
            continue
        stripped = name.strip()
        if not stripped:
            continue
        passcode = str(card_id)
        mapping.setdefault(passcode, stripped)
    return mapping


def _cache_path(cache_dir):
    if not cache_dir:
        return None
    return os.path.join(cache_dir, DEFAULT_CACHE_FILENAME)


def _load_cache(cache_dir):
    path = _cache_path(cache_dir)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(cache_dir, payload):
    path = _cache_path(cache_dir)
    if not path:
        return
    os.makedirs(cache_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, path)


def fetch_catalogue(
    session=None,
    timeout=DEFAULT_TIMEOUT,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    retry_pacing_seconds=DEFAULT_RETRY_PACING_SECONDS,
    sleep=time.sleep,
):
    """
    Perform one HTTPS GET against the YGOPRODeck cardinfo endpoint and
    return the parsed JSON payload (a dict with a ``data`` array).

    Transient failures (``requests.RequestException``) are retried up to
    ``max_attempts`` times with a fixed pacing delay between attempts.
    Non-list ``data`` payloads or missing ``data`` keys raise
    ``CatalogueError`` immediately -- we never guess a schema.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    client = session or requests.Session()
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.get(
                YGOPRODECK_CARDINFO_URL,
                timeout=timeout,
                verify=True,
            )
            response.raise_for_status()
            payload = response.json()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise CatalogueError(
                    f"YGOPRODeck cardinfo endpoint failed after {attempt} attempt(s): {exc}"
                ) from exc
            if retry_pacing_seconds > 0:
                sleep(retry_pacing_seconds)
        except ValueError as exc:  # invalid JSON
            raise CatalogueError(f"YGOPRODeck cardinfo returned invalid JSON: {exc}") from exc
    else:  # pragma: no cover -- loop always breaks or raises
        raise CatalogueError(f"YGOPRODeck cardinfo endpoint failed: {last_exc}")

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CatalogueError(
            "YGOPRODeck cardinfo response missing expected 'data' array"
        )
    return payload


def load_passcode_map(
    cache_dir,
    session=None,
    timeout=DEFAULT_TIMEOUT,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    retry_pacing_seconds=DEFAULT_RETRY_PACING_SECONDS,
    sleep=time.sleep,
    force_refresh=False,
):
    """
    Return ``(passcode_map, source)`` where ``source`` is ``"cache"`` if
    the catalogue was reused from the local cache, or ``"api"`` if it was
    fetched fresh (and then written to the cache for later reuse).

    If the API call fails and no cache is available, ``CatalogueError``
    propagates so callers can honestly report the failure and leave the
    dataset untouched.
    """
    if not force_refresh:
        cached = _load_cache(cache_dir)
        if cached is not None and isinstance(cached.get("data"), list):
            return build_passcode_map(cached["data"]), "cache"

    payload = fetch_catalogue(
        session=session,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_pacing_seconds=retry_pacing_seconds,
        sleep=sleep,
    )
    if cache_dir:
        _write_cache(cache_dir, payload)
    return build_passcode_map(payload["data"]), "api"


def resolve_zone(entries, passcode_map):
    """
    Resolve one deck-zone list ``[{"name": ..., "count": N}, ...]`` into
    ``(resolved_entries, unresolved_entries)``.

    - Non-numeric ``name`` values are treated as already-canonical and
      passed through unchanged (this preserves legacy/non-YGOPRODeck
      names and any name the collector might supply directly in the
      future).
    - Numeric passcode names are looked up in ``passcode_map`` and
      rewritten to the canonical card name. Copy counts are preserved.
      Two passcodes that map to the same canonical name (defensive) are
      merged and their counts summed.
    - Any numeric passcode not in the map is returned in
      ``unresolved_entries`` (``{"passcode": str, "count": int}``) and is
      NOT included in ``resolved_entries`` -- callers must refuse to
      persist a partial deck.
    """
    resolved_order = []
    resolved_counts = {}
    unresolved = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        count = entry.get("count", 0)
        if not isinstance(count, int) or count <= 0:
            continue
        if _is_numeric_passcode(name):
            canonical = passcode_map.get(name)
            if canonical is None:
                unresolved.append({"passcode": name, "count": count})
                continue
            key = canonical
        elif isinstance(name, str) and name.strip():
            key = name.strip()
        else:
            # Malformed entry -- report as unresolved rather than silently drop.
            unresolved.append({"passcode": str(name), "count": count})
            continue
        if key in resolved_counts:
            resolved_counts[key] += count
        else:
            resolved_order.append(key)
            resolved_counts[key] = count
    resolved_entries = [{"name": n, "count": resolved_counts[n]} for n in resolved_order]
    return resolved_entries, unresolved


def resolve_observation_cards(observation, passcode_map):
    """
    Resolve all three deck zones on an observation dict.

    Returns ``(new_observation, unresolved_by_zone)``:
    - ``new_observation`` is a shallow copy with ``main_deck``,
      ``side_deck``, ``extra_deck`` rewritten to canonical names. When
      any zone has unresolved passcodes ``new_observation`` is ``None``
      (the caller must refuse to substitute another card or produce a
      partial deck).
    - ``unresolved_by_zone`` is ``{zone: [{"passcode","count"}, ...]}``,
      omitting zones that were fully resolved.
    """
    unresolved_by_zone = {}
    resolved_zones = {}
    for zone in ("main_deck", "side_deck", "extra_deck"):
        resolved, unresolved = resolve_zone(observation.get(zone) or [], passcode_map)
        resolved_zones[zone] = resolved
        if unresolved:
            unresolved_by_zone[zone] = unresolved
    if unresolved_by_zone:
        return None, unresolved_by_zone
    new_obs = dict(observation)
    new_obs.update(resolved_zones)
    return new_obs, {}


def observation_has_numeric_passcodes(observation):
    """
    True if any deck zone contains at least one entry whose ``name`` is
    a pure-digit passcode string. Used by the backfill CLI to skip
    already-canonical observations so it is idempotent.
    """
    for zone in ("main_deck", "side_deck", "extra_deck"):
        for entry in observation.get(zone) or []:
            if isinstance(entry, dict) and _is_numeric_passcode(entry.get("name")):
                return True
    return False
