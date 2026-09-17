"""
YGOPRODeck card-identity bridge.

Deck arrays returned by YGOPRODeck's ``getDecks.php`` catalogue contain
numeric card ids (passcodes). Meta Watch's price data (data/prices.db)
tracks printings by ``card_name``. To reconcile the two without ever
guessing, this module maintains a passcode -> canonical card name map,
sourced exclusively from YGOPRODeck's official ``cardinfo.php`` API.

Design invariants:

- **HTTPS only, verified.** The single ``cardinfo.php`` request uses
  ``https://`` with ``requests``'s default certificate verification
  (``verify=True``). It is never overridden.
- **One call per run.** ``resolve_passcodes`` fetches ``cardinfo.php`` at
  most once, and only if the cache cannot already answer every requested
  passcode. Repeat resolutions within the same run reuse the in-memory
  map; repeat runs reuse the on-disk cache file.
- **Local cache.** The cache lives at ``data/ygoprodeck_card_cache.json``
  by default. It is an append-only mapping of ``passcode -> canonical
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


def _normalize_passcode(value):
    """Return the string form of a numeric passcode, or None if not usable."""
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
    """Load the passcode -> canonical name cache, or an empty dict."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    mapping = payload.get("passcode_to_name") if isinstance(payload, dict) else None
    if not isinstance(mapping, dict):
        return {}
    # Normalize keys to strings; keep only non-empty string values.
    normalized = {}
    for key, value in mapping.items():
        norm_key = _normalize_passcode(key)
        if norm_key is None:
            continue
        if isinstance(value, str) and value.strip():
            normalized[norm_key] = value.strip()
    return normalized


def save_cache(cache, cache_path=DEFAULT_CACHE_PATH):
    """Atomically persist the passcode -> canonical name cache."""
    directory = os.path.dirname(cache_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "generated_at": _iso_now(),
        "source": CARDINFO_API_URL,
        "passcode_to_name": {str(k): v for k, v in sorted(cache.items())},
    }
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, cache_path)


def _extract_passcode_map(payload):
    """
    Build a passcode -> canonical name map from a ``cardinfo.php`` payload.

    YGOPRODeck's response is ``{"data": [ {"id": 89631139, "name": "Blue-Eyes
    White Dragon", ...}, ... ]}``. Each entry's ``id`` is the card's Konami
    passcode, and ``name`` is the canonical English card name we want to
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
        passcode = _normalize_passcode(entry.get("id"))
        name = entry.get("name")
        if passcode is None or not isinstance(name, str) or not name.strip():
            continue
        mapping[passcode] = name.strip()
    if not mapping:
        raise CardBridgeError("cardinfo payload produced no id -> name entries")
    return mapping


def fetch_cardinfo_map(session=None, timeout=DEFAULT_TIMEOUT):
    """
    Perform the single HTTPS ``cardinfo.php`` fetch and return the parsed
    passcode -> canonical name map. Raises :class:`CardBridgeError` on any
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
    return _extract_passcode_map(payload)


def resolve_passcodes(
    passcodes,
    cache_path=DEFAULT_CACHE_PATH,
    session=None,
    timeout=DEFAULT_TIMEOUT,
    fetch=None,
    persist=True,
):
    """
    Resolve every requested passcode against the on-disk cache, calling the
    cardinfo endpoint **at most once** to fill any gaps.

    Args:
        passcodes: iterable of passcode strings/ints to resolve.
        cache_path: JSON cache path (created if missing).
        session: optional ``requests``-compatible session for the fetch.
        timeout: HTTP timeout for the single fetch.
        fetch: optional dependency-injection hook -- callable returning a
            ``passcode -> name`` dict, used by tests to avoid real HTTP.
        persist: when True (default) and a fetch occurred, rewrite the
            cache file atomically. Set to False to leave the file alone.

    Returns:
        ``(resolved, unresolved, fetched)`` where ``resolved`` is a dict of
        the passcodes that mapped to a canonical name, ``unresolved`` is a
        sorted list of the ones that did not, and ``fetched`` is True if
        this call actually hit the endpoint (i.e. incurred a network
        request).

    Raises:
        CardBridgeError: if a fetch was needed but the endpoint failed.
        The cache file is not modified in that case.
    """
    normalized_requests = []
    seen = set()
    for raw in passcodes:
        norm = _normalize_passcode(raw)
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
        cache.update({_normalize_passcode(k) or str(k): v for k, v in new_map.items() if v})
        fetched = True
        if persist:
            save_cache(cache, cache_path)

    resolved = {}
    unresolved = []
    for passcode in normalized_requests:
        name = cache.get(passcode)
        if name:
            resolved[passcode] = name
        else:
            unresolved.append(passcode)
    unresolved.sort()
    return resolved, unresolved, fetched


def rebuild_cards_with_canonical_names(cards, resolved):
    """
    Rewrite a list of ``{name, count}`` deck entries in-place-ish, replacing
    numeric passcode ``name`` values with their canonical card name.

    Returns ``(new_cards, unresolved_ids)``. Non-numeric names pass through
    unchanged (they were already canonical). Counts are preserved. When two
    passcodes happen to map to the same canonical name (very rare, but
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
        passcode = _normalize_passcode(raw_name)
        if passcode is None:
            # Already a non-numeric (canonical) name -- keep as-is.
            canonical = raw_name if isinstance(raw_name, str) else str(raw_name)
        else:
            canonical = resolved.get(passcode)
            if canonical is None:
                unresolved.append(passcode)
                continue
        if canonical not in counts:
            ordered_names.append(canonical)
            counts[canonical] = 0
        counts[canonical] += count
    return (
        [{"name": name, "count": counts[name]} for name in ordered_names],
        unresolved,
    )
