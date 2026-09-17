"""
Collect newly published tournament decklists for Meta Watch from official
Konami blog sources, validate them, and import them into the dataset.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, ".")

from app.meta_watch import FORMAT_TCG_ADVANCED, load_dataset, revision_key, validate_observation  # noqa: E402
from scripts.import_meta_watch_lists import import_observations_payload  # noqa: E402

DEFAULT_DATASET_PATH = "data/meta_watch_lists.json"
DEFAULT_REPORT_PATH = "data/meta_watch_collection_report.json"
DEFAULT_SOURCE_INDEXES = (
    {
        "name": "Konami Blog YCS",
        "url": "https://yugiohblog.konami.com/category/ycs/",
        "format": FORMAT_TCG_ADVANCED,
        "region": "NA",
    },
    {
        "name": "Konami Blog Championships",
        "url": "https://yugiohblog.konami.com/category/championships/",
        "format": FORMAT_TCG_ADVANCED,
        "region": "NA",
    },
)
DECK_LIST_LINK_HINTS = ("deck-list", "deck-lists")
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HEADING_BLOCK_RE = re.compile(r"<h([2-6])[^>]*>(.*?)</h\1>(.*?)(?=<h[2-6][^>]*>|$)", re.IGNORECASE | re.DOTALL)
_DECK_COUNT_RE = re.compile(r"(main|side|extra)\s*deck\s*:\s*(\d+)", re.IGNORECASE)
_CARD_COUNT_PREFIX_RE = re.compile(r"^(\d+)\s*[x×-]?\s+(.+?)\s*$")
_CARD_COUNT_SUFFIX_RE = re.compile(r"^(.+?)\s*[x×]\s*(\d+)\s*$", re.IGNORECASE)
_PLACEMENT_RE = re.compile(r"\b(\d+(?:st|nd|rd|th)\s+Place)\b", re.IGNORECASE)
_PUBLISHED_META_RE = re.compile(
    r"""<meta[^>]+(?:property|itemprop)=['"](article:published_time|datePublished)['"][^>]+content=['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_TIME_DATETIME_RE = re.compile(r"""<time[^>]*datetime=['"]([^'"]+)['"][^>]*>""", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_tags(html):
    text = _TAG_RE.sub("\n", html)
    return unescape(text)


def _clean_line(text):
    return " ".join(text.replace("\xa0", " ").strip().split())


def _slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "unknown-event"


def _parse_iso_datetime(raw):
    value = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_article_links(index_html, base_url):
    parsed_base = urlparse(base_url)
    links = []
    seen = set()
    for raw in _HREF_RE.findall(index_html):
        absolute = urljoin(base_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or parsed.netloc != parsed_base.netloc:
            continue
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") + "/"
        path_lower = parsed.path.lower()
        if not any(hint in path_lower for hint in DECK_LIST_LINK_HINTS):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def _extract_article_title(article_html, article_url):
    m = re.search(r"<title[^>]*>(.*?)</title>", article_html, re.IGNORECASE | re.DOTALL)
    if not m:
        return article_url
    text = _clean_line(_strip_tags(m.group(1)))
    text = re.sub(r"\s*[-|]\s*yu-gi-oh!\s*tcg event coverage\s*$", "", text, flags=re.IGNORECASE)
    return text


def _extract_published_at(article_html):
    for pattern in (_PUBLISHED_META_RE, _TIME_DATETIME_RE):
        match = pattern.search(article_html)
        if match:
            raw_value = match.group(2) if pattern is _PUBLISHED_META_RE else match.group(1)
            parsed = _parse_iso_datetime(raw_value)
            if parsed:
                return parsed
    return None


def _extract_published_date(article_html, published_at):
    if published_at:
        return published_at[:10]
    match = _DATE_RE.search(_strip_tags(article_html))
    return match.group(1) if match else None


def _parse_header(header_text):
    cleaned = _clean_line(header_text)
    placement = None
    placement_match = _PLACEMENT_RE.search(cleaned)
    if placement_match:
        placement = placement_match.group(1)
        cleaned = cleaned.replace(placement_match.group(0), "").strip(" :-–—|")

    archetype = "UNKNOWN"
    player = cleaned
    for sep in (" - ", " – ", " — ", " | "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            player = left.strip()
            if right.strip():
                archetype = right.strip()
            break
    return player, archetype, placement


def _parse_cards_from_lines(lines):
    cards = []
    for line in lines:
        line = _clean_line(line)
        if not line:
            continue
        m = _CARD_COUNT_PREFIX_RE.match(line)
        if m:
            cards.append({"name": m.group(2).strip(), "count": int(m.group(1))})
            continue
        m = _CARD_COUNT_SUFFIX_RE.match(line)
        if m:
            cards.append({"name": m.group(1).strip(), "count": int(m.group(2))})
    return cards


def _parse_deck_sections(block_html):
    lines = [_clean_line(line) for line in _strip_tags(block_html).splitlines()]
    expected = {}
    parsed = {"main_deck": [], "side_deck": [], "extra_deck": []}
    current_zone = None
    current_lines = []

    def flush():
        nonlocal current_lines, current_zone
        if current_zone:
            parsed[current_zone].extend(_parse_cards_from_lines(current_lines))
        current_zone = None
        current_lines = []

    for line in lines:
        if not line:
            continue
        count_match = _DECK_COUNT_RE.search(line)
        if count_match:
            flush()
            zone_key = f"{count_match.group(1).lower()}_deck"
            expected[zone_key] = int(count_match.group(2))
            current_zone = zone_key
            continue
        if current_zone:
            current_lines.append(line)
    flush()
    return expected, parsed


def _event_id_from_url(url):
    path = urlparse(url).path.strip("/")
    slug = path.split("/")[-1] if path else "unknown-event"
    return _slugify(slug)


def _deck_signature(observation):
    sig = {}
    for zone in ("main_deck", "side_deck", "extra_deck"):
        rows = tuple(sorted((c["name"], int(c["count"])) for c in observation.get(zone, [])))
        sig[zone] = rows
    return sig


def parse_article_observations(article_html, article_url, source):
    title = _extract_article_title(article_html, article_url)
    published_at = _extract_published_at(article_html)
    published_date = _extract_published_date(article_html, published_at)
    if not published_date:
        return [], [{"source_url": article_url, "reason": "missing publication date"}]

    event_id = _event_id_from_url(article_url)
    event_name = title
    event_date = published_date
    format_name = source["format"]
    region = source["region"]
    banlist_id = f"UNKNOWN-{published_date[:7]}"
    first_seen = _now_iso()

    observations = []
    rejected = []
    for _, header_html, block_html in _HEADING_BLOCK_RE.findall(article_html):
        if not _DECK_COUNT_RE.search(block_html):
            continue
        header_text = _strip_tags(header_html)
        player, archetype, placement = _parse_header(header_text)
        if not player:
            rejected.append({"source_url": article_url, "reason": "missing player identity in heading"})
            continue

        expected, parsed = _parse_deck_sections(block_html)
        if "main_deck" not in expected:
            rejected.append({"source_url": article_url, "player": player, "reason": "missing Main Deck count"})
            continue

        main_total = sum(c["count"] for c in parsed["main_deck"])
        side_total = sum(c["count"] for c in parsed["side_deck"])
        extra_total = sum(c["count"] for c in parsed["extra_deck"])
        if main_total != expected.get("main_deck", 0):
            rejected.append(
                {
                    "source_url": article_url,
                    "player": player,
                    "reason": f"main_deck count mismatch parsed={main_total} expected={expected.get('main_deck')}",
                }
            )
            continue
        if "side_deck" in expected and side_total != expected["side_deck"]:
            rejected.append(
                {
                    "source_url": article_url,
                    "player": player,
                    "reason": f"side_deck count mismatch parsed={side_total} expected={expected['side_deck']}",
                }
            )
            continue
        if "extra_deck" in expected and extra_total != expected["extra_deck"]:
            rejected.append(
                {
                    "source_url": article_url,
                    "player": player,
                    "reason": f"extra_deck count mismatch parsed={extra_total} expected={expected['extra_deck']}",
                }
            )
            continue

        observation = {
            "event_id": event_id,
            "event_name": event_name,
            "event_date": event_date,
            "region": region,
            "format": format_name,
            "banlist_id": banlist_id,
            "player": player,
            "placement": placement,
            "archetype": archetype or "UNKNOWN",
            "source_url": article_url,
            "source_type": "tournament",
            "source_provider": "konami_blog",
            "published_at": published_at,
            "first_seen_at": first_seen,
            "main_deck": parsed["main_deck"],
            "side_deck": parsed["side_deck"],
            "extra_deck": parsed["extra_deck"],
        }
        errors = validate_observation(observation)
        if errors:
            rejected.append({"source_url": article_url, "player": player, "reason": "; ".join(errors)})
            continue
        observations.append(observation)

    if not observations and not rejected:
        rejected.append({"source_url": article_url, "reason": "no parseable decklist blocks found"})
    return observations, rejected


def _write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def collect_and_import(
    dataset_path=DEFAULT_DATASET_PATH,
    report_path=DEFAULT_REPORT_PATH,
    dry_run=False,
    timeout=20,
    sources=None,
    session=None,
):
    client = session or requests.Session()
    sources = tuple(sources or DEFAULT_SOURCE_INDEXES)
    existing = load_dataset(dataset_path)
    existing_by_key = {revision_key(obs): obs for obs in existing.get("observations", [])}
    for revision in existing.get("revisions") or []:
        key = revision_key(revision)
        if key in existing_by_key:
            existing_by_key[key] = revision

    source_failures = []
    article_failures = []
    rejected_records = []
    changed_source_lists = []
    collected = []
    seen_collected_keys = set()
    duplicate_existing_precheck_skipped = 0
    duplicate_in_batch_precheck_skipped = 0
    article_sources = {}

    for source in sources:
        source_url = source["url"]
        try:
            response = client.get(source_url, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            source_failures.append({"source_url": source_url, "error": str(exc)})
            continue

        links = extract_article_links(response.text, source_url)
        for link in links:
            article_sources.setdefault(link, source)

    for article_url, source in article_sources.items():
        try:
            response = client.get(article_url, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            article_failures.append({"source_url": article_url, "error": str(exc)})
            continue

        observations, rejected = parse_article_observations(response.text, article_url, source)
        rejected_records.extend(rejected)
        for obs in observations:
            key = revision_key(obs)
            existing_obs = existing_by_key.get(key)
            if existing_obs and existing_obs.get("source_url") == obs.get("source_url"):
                if _deck_signature(existing_obs) != _deck_signature(obs):
                    changed_source_lists.append(
                        {
                            "event_id": obs.get("event_id"),
                            "player": obs.get("player"),
                            "source_url": obs.get("source_url"),
                            "existing_first_seen_at": existing_obs.get("first_seen_at"),
                        }
                    )
            if key in seen_collected_keys:
                duplicate_in_batch_precheck_skipped += 1
                continue
            seen_collected_keys.add(key)
            collected.append(obs)

    import_result = import_observations_payload({"observations": collected}, dataset_path=dataset_path, dry_run=dry_run)
    report = {
        "collected_at": _now_iso(),
        "dataset_path": dataset_path,
        "dry_run": dry_run,
        "sources_checked": len(sources),
        "source_failures": source_failures,
        "source_articles_checked": len(article_sources),
        "source_article_failures": article_failures,
        "candidate_observations": len(collected),
        "duplicate_existing_precheck_skipped": duplicate_existing_precheck_skipped,
        "duplicate_in_batch_precheck_skipped": duplicate_in_batch_precheck_skipped,
        "rejected_records": rejected_records,
        "changed_source_lists": changed_source_lists,
        "import": import_result,
    }
    _write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Collect and import Meta Watch decklists from official Konami sources.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH, help="Path to data/meta_watch_lists.json")
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH, help="Where to write collection/import report JSON")
    parser.add_argument("--dry-run", action="store_true", help="Collect/validate/report without writing dataset changes")
    parser.add_argument("--timeout", default=20, type=int, help="HTTP timeout seconds per request")
    args = parser.parse_args()

    result = collect_and_import(
        dataset_path=args.dataset,
        report_path=args.report,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
