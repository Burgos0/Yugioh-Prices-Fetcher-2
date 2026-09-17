"""
YGOPRODeck card-identity bridge.

Deck arrays returned by YGOPRODeck's ``getDecks.php`` catalogue contain
numeric card ids (Konami card codes). Meta Watch's price data (data/prices.db)
tracks printings by ``card_name``. To reconcile the two without ever
guessing, this module maintains a card_id -> canonical card name map,
sourced exclusively from YGOPRODeck's official ``cardinfo.php`` API.

Design invariants:

- **HTTPS only, verified.** The single ``cardinfo.php`` request uses
  ``https://`` with ``requests``'s default certificate verification
  (``verify=True``). It is never overridden.
- **One call per run.** ``resolve_card_ids`` fetches ``cardinfo.php`` at
  most once, and only if the cache cannot already answer every requested
  card_id. Repeat resolutions within the same run reuse the in-memory
  map; repeat runs reuse the on-disk cache file.
- **Local cache.** The cache lives at ``data/ygoprodeck_card_cache.json``
  by default. It is an append-only mapping of ``card_id -> canonical
  name``. A cache miss triggers a single fetch that repopulates the map
  for every card YGOPRODeck knows about, then the cache is atomically
  rewritten.
- **Honest failure.** If the endpoint fails or returns malformed data,
  the cache is not modified and callers get an explicit failure. Callers
  must then choose to reject the observation rather than invent a name.
- **No fuzzy matching.** The only mapping is the exact numeric id ->
  canonical name shipped by YGOPRODeck for that card. One card can (and
  frequently does) correspond to multiple *printings* in prices.db --
  that many-to-one downstream fan-out is handled by
  ``app.meta_watch.resolve_card_printings`` and is deliberately out of
  scope here.
"""
import json
import os
from datetime import datetime, timezone

import requests

CARDINFO_API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
DEFAULT_CACHE_PATH = "data/ygoprodeck_card_cache.json"
DEFAULT_TIMEOUT = 60


class CardBridgeError(RuntimeError):
    """Raised when the cardinfo endpoint cannot be used honestly."""


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_card_id(value):
    """Return the string form of a numeric card_id, or None if not usable."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int in Python; reject explicitly.
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # We only bridge purely numeric ids -- anything else is either
        # already a canonical name or garbage, and must not be silently
        # rewritten.
        return text if text.isdigit() else None
    return None


def load_cache(cache_path=DEFAULT_CACHE_PATH):
    """Load the card_id -> canonical name cache, or an empty dict."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    mapping = payload.get("card_id_to_name") if isinstance(payload, dict) else None
    if not isinstance(mapping, dict):
        return {}
    # Normalize keys to strings; keep only non-empty string values.
    normalized = {}
    for key, value in mapping.items():
        norm_key = _normalize_card_id(key)
        if norm_key is None:
            continue
        if isinstance(value, str) and value.strip():
            normalized[norm_key] = value.strip()
    return normalized


def save_cache(cache, cache_path=DEFAULT_CACHE_PATH):
    """Atomically persist the card_id -> canonical name cache."""
    directory = os.path.dirname(cache_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "generated_at": _iso_now(),
        "source": CARDINFO_API_URL,
        "card_id_to_name": {str(k): v for k, v in sorted(cache.items())},
    }
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, cache_path)


def _extract_card_id_map(payload):
    """
    Build a card_id -> canonical name map from a ``cardinfo.php`` payload.

    YGOPRODeck's response is ``{"data": [ {"id": 89631139, "name": "Blue-Eyes
    White Dragon", ...}, ... ]}``. Each entry's ``id`` is the card's Konami
    card_id, and ``name`` is the canonical English card name we want to
    align with prices.db's ``card_name``.
    """
    if not isinstance(payload, dict):
        raise CardBridgeError("cardinfo payload is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise CardBridgeError("cardinfo payload missing 'data' list")
    mapping = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        card_id = _normalize_card_id(entry.get("id"))
        name = entry.get("name")
        if card_id is None or not isinstance(name, str) or not name.strip():
            continue
        mapping[card_id] = name.strip()
    if not mapping:
        raise CardBridgeError("cardinfo payload produced no id -> name entries")
    return mapping


def fetch_cardinfo_map(session=None, timeout=DEFAULT_TIMEOUT):
    """
    Perform the single HTTPS ``cardinfo.php`` fetch and return the parsed
    card_id -> canonical name map. Raises :class:`CardBridgeError` on any
    transport or payload problem so callers can leave state untouched.
    """
    client = session or requests.Session()
    try:
        response = client.get(CARDINFO_API_URL, timeout=timeout, verify=True)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise CardBridgeError(f"cardinfo request failed: {exc}") from exc
    except ValueError as exc:
        raise CardBridgeError(f"cardinfo payload was not valid JSON: {exc}") from exc
    return _extract_card_id_map(payload)


def resolve_card_ids(
    card_ids,
    cache_path=DEFAULT_CACHE_PATH,
    session=None,
    timeout=DEFAULT_TIMEOUT,
    fetch=None,
    persist=True,
):
    """
    Resolve every requested card_id against the on-disk cache, calling the
    cardinfo endpoint **at most once** to fill any gaps.

    Args:
        card_ids: iterable of card_id strings/ints to resolve.
        cache_path: JSON cache path (created if missing).
        session: optional ``requests``-compatible session for the fetch.
        timeout: HTTP timeout for the single fetch.
        fetch: optional dependency-injection hook -- callable returning a
            ``card_id -> name`` dict, used by tests to avoid real HTTP.
        persist: when True (default) and a fetch occurred, rewrite the
            cache file atomically. Set to False to leave the file alone.

    Returns:
        ``(resolved, unresolved, fetched)`` where ``resolved`` is a dict of
        the card_ids that mapped to a canonical name, ``unresolved`` is a
        sorted list of the ones that did not, and ``fetched`` is True if
        this call actually hit the endpoint (i.e. incurred a network
        request).

    Raises:
        CardBridgeError: if a fetch was needed but the endpoint failed.
        The cache file is not modified in that case.
    """
    normalized_requests = []
    seen = set()
    for raw in card_ids:
        norm = _normalize_card_id(raw)
        if norm is None or norm in seen:
            continue
        seen.add(norm)
        normalized_requests.append(norm)

    cache = load_cache(cache_path)
    missing = [p for p in normalized_requests if p not in cache]
    fetched = False

    if missing:
        fetcher = fetch or (lambda: fetch_cardinfo_map(session=session, timeout=timeout))
        new_map = fetcher()
        if not isinstance(new_map, dict) or not new_map:
            raise CardBridgeError("card bridge fetch returned no entries")
        # Merge into the cache. Existing entries are preserved; the fresh
        # payload wins on collisions since it's the newest authority.
        cache.update({_normalize_card_id(k) or str(k): v for k, v in new_map.items() if v})
        fetched = True
        if persist:
            save_cache(cache, cache_path)

    resolved = {}
    unresolved = []
    for card_id in normalized_requests:
        name = cache.get(card_id)
        if name:
            resolved[card_id] = name
        else:
            unresolved.append(card_id)
    unresolved.sort()
    return resolved, unresolved, fetched


def rebuild_cards_with_canonical_names(cards, resolved):
    """
    Rewrite a list of ``{name, count}`` deck entries in-place-ish, replacing
    numeric card_id ``name`` values with their canonical card name.

    Returns ``(new_cards, unresolved_ids)``. Non-numeric names pass through
    unchanged (they were already canonical). Counts are preserved. When two
    card_ids happen to map to the same canonical name (very rare, but
    possible for alt-art reprints sharing a name), their counts are
    summed and the first-seen order is kept.
    """
    unresolved = []
    ordered_names = []
    counts = {}
    for entry in cards or []:
        raw_name = entry.get("name")
        count = entry.get("count", 0)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        card_id = _normalize_card_id(raw_name)
        if card_id is None:
            # Already a non-numeric (canonical) name -- keep as-is.
            canonical = raw_name if isinstance(raw_name, str) else str(raw_name)
        else:
            canonical = resolved.get(card_id)
            if canonical is None:
                unresolved.append(card_id)
                continue
        if canonical not in counts:
            ordered_names.append(canonical)
            counts[canonical] = 0
        counts[canonical] += count
    return (
        [{"name": name, "count": counts[name]} for name in ordered_names],
        unresolved,
    )
