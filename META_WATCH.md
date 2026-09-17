# Meta Watch: research-only published-list adoption tracking

Tracks whether cards are gaining adoption in **published tournament
decklists** while their tracked TCGPlayer price stays relatively flat.
This is explicitly research-only: it never labels anything a price
prediction, never recommends a purchase, and never shows a confidence
probability. It does not change Early Movers, Top Gainers/Losers, or the
production database snapshot pipeline in any way.

## Data source

Meta Watch now includes an unattended collector that starts from official
Konami blog index pages and follows decklist article links:

- https://yugiohblog.konami.com/category/ycs/
- https://yugiohblog.konami.com/category/championships/

Collection fetches only over HTTPS with normal certificate verification
(`requests` defaults); TLS verification is never disabled.

## Sourced sample (imported)

6 tournament decklist observations were manually transcribed from 3 real,
official Konami blog "Top 32 Deck Lists" articles (TCG Advanced format),
verifying player, placement, and Main/Side/Extra counts against each
source page before import:

| Event | Published | Source | Lists imported |
| --- | --- | --- | --- |
| YCS Montr\u00e9al (Advanced Format) | 2026-08-16 | https://yugiohblog.konami.com/2026/ycs/2026-08-quebec/ycs-montreal-top-32-deck-lists/ | Francisco Andres Osorio Bobadilla (1st), Julien Leo Kehon (2nd) |
| North America WCQ | 2026-07-12 | https://yugiohblog.konami.com/2026/championships/north-america-wcq-top-32-deck-lists/ | Ryan Linus ON Yu (1st), Charley Ray Futch III (2nd) |
| 300th YCS in Virginia (Advanced Format) | 2026-02-15 | https://yugiohblog.konami.com/2026/ycs/2026-02-300th-na/300th-ycs-in-virginia-top-32-deck-lists-advanced-format/ | Steven John Logan (3rd), Steven Patrick Gleason (4th) |

Each list's Main/Side/Extra card counts were summed and checked against
the article's own stated `Main Deck: N` / `Side Deck: 15` / `Extra Deck:
15` totals before import; all 6 matched exactly. Import input file:
`scripts/meta_watch_sample_input_2026-09-15.json` (re-runnable via the
importer, e.g. to re-verify or re-import into a fresh dataset).

Fields the source articles did not label are recorded as explicit
`"UNKNOWN"` (archetype) or `"UNKNOWN-YYYY-MM"` (banlist_id, kept distinct
per event's known month so three different real-world banlist periods are
never silently merged into one bucket) -- never guessed. `published_at`
uses each article's stated publish date (its only available timestamp);
`first_seen_at` is the actual UTC time this batch was imported.

### Card identity matching note

19 of the 160 distinct card names in this sample initially failed to
resolve against `prices.db` -- all because the Konami blog renders card
names with an en dash (`\u2013`) where the tracked printing in `prices.db`
uses a plain hyphen (e.g. "Sky Striker Ace \u2013 Kagari" vs "Sky Striker
Ace - Kagari"). This is punctuation-encoding noise, not a real identity
ambiguity, so `app/meta_watch.py` normalizes dash characters as a
fallback *only* after an exact/prefix match fails
(`resolve_card_printings_with_fallback`/`build_normalized_name_index`).
This normalization never merges two different card names -- it only
canonicalizes dash punctuation before re-applying the same exact/prefix
rule. After this fix, all 160 names in the sample resolve to a tracked
printing; 0 remain unresolved.

## What was built

- `app/meta_watch.py`: adoption/price-context analysis library (pure
  functions over a local JSON dataset + read-only `prices.db`; no network
  calls, so no live requests happen during page loads).
- `scripts/import_meta_watch_lists.py`: validates and imports a batch of
  decklist observations into the versioned dataset. See its module
  docstring for the exact input JSON schema.
- `data/meta_watch_lists.json`: the versioned, append-only dataset of raw
  imported observations (kept separate from `prices.db`/`signals.db`).
  Ships empty (`"observations": []`).
- `/meta-watch` route + nav link, rendering:
  - Overall published-list adoption stats per card (lists containing it /
    total observed lists, percentage-point change, average copies,
    distinct supporting events, Main/Side/Extra usage).
  - Within-archetype adoption breakdown alongside overall adoption.
  - Price context (latest price, freshness in days, 7-day % change, an
    experimental "flat" flag at a configurable threshold) for cards whose
    decklist name resolves to a tracked printing; unresolved names are
    reported, never guessed.
  - Explicit insufficient-data states (below the configurable minimum
    lists/events per window) and an explicit "no data imported yet" empty
    state -- the shipped empty dataset renders this state, not fixture
    data.
- `tests/test_meta_watch.py`: covers validation, dedup, Main/Side/Extra
  counts, denominators/adoption %, copy averages, within-archetype
  adoption, minimum-sample handling, format separation, publication-time
  cutoff (published_at over first_seen_at), unresolved/ambiguous card
  matching, missing/stale price history, the importer's reject/dedupe
  behavior, and page rendering (including that the real, empty dataset
  renders the empty state rather than any fixture-looking content).

## Rules encoded in the analysis

- Only `source_type: "tournament"` observations count; casual uploads are
  excluded from adoption stats but not silently dropped from the dataset.
- Same player + same event is deduplicated, keeping the first import.
- Formats/banlists are never combined: TCG Advanced, OCG, Master Duel, Rush
  Duel, and `OTHER` are tracked as separate `format` values. Each
  `banlist_id` is evaluated as its own pair of 14-day windows.
- `build_meta_watch_report(...)` now records `format_population_counts` and
  `non_target_format_counts` so each format population is visible separately
  and excluded populations are explicit in every report.
- Publication-time cutoff prefers `published_at`; falls back to
  `first_seen_at` (the import time) only when no publication date is
  known.
- A card with zero appearances in the prior window is reported separately
  as "newly observed," never as an infinite percentage increase.
- Insufficient-data thresholds (minimum lists/events per window, the
  adoption window length, and the "flat price" percentage threshold) are
  all function parameters with documented defaults in `app/meta_watch.py`,
  not hardcoded.
- Printing selection for price context is based only on which printing has
  the most price history (a neutral, non-performance-based tiebreaker),
  never on which printing's price moved a particular way.

## Import format and workflow

See the module docstring in `scripts/import_meta_watch_lists.py` for the
full JSON schema. Usage:

```bash
python -m scripts.import_meta_watch_lists path/to/sourced_batch.json
python -m scripts.import_meta_watch_lists path/to/sourced_batch.json --dry-run
python -m scripts.collect_meta_watch_lists --dry-run
```

Automated scheduling is in `.github/workflows/meta_watch_daily.yml`. It
collects/parses decklists, validates/imports observations, writes
`data/meta_watch_collection_report.json` (import counts, rejected records,
source failures, and changed-source flags), and opens/updates an automated
data-update PR instead of pushing directly to `main`.

## TopDeck coverage check (pre-integration gate)

Before any production integration with a TopDeck source, run the manual
coverage check workflow `.github/workflows/topdeck_coverage_check.yml`.
It uses the `TOPDECK_API_KEY` secret, performs a **read-only** query for
`game="Yu-Gi-Oh"` and `format="Advanced"` over the last 90 days, and
uploads a sanitized artifact report (no secrets, no production dataset
writes) with:

- events found (dates + participant counts where present)
- player decklist completeness split (complete Main/Side/Extra vs external
  links vs missing/incomplete)
- paper TCG Advanced distinction assessment
- explicit publication timestamp availability (never substituted from event
  date)
- coverage windows for the last 14/30/90 days

## YGOPRODeck source evaluation experiment (pre-decision only)

`scripts/check_ygoprodeck_tcg_coverage.py` and
`.github/workflows/ygoprodeck_coverage_check.yml` provide a read-only
coverage experiment for curated **Tournament Meta Decks (TCG)**. This is
for source-selection evidence only (no production data writes).

- Uses YGOPRODeck's observed deck feed endpoint:
  `https://ygoprodeck.com/api/decks/getDecks.php` with
  `_sft_category=Tournament Meta Decks`
- Reports 14/30/90-day event and decklist coverage, metadata completeness,
  timestamp quality, and excluded non-paper records.
- Explicitly excludes OCG, Master Duel, Genesys, online/remote/casual/test
  markers from paper TCG counts.

Because this endpoint is not part of YGOPRODeck's documented card API guide,
results from this check are used to evaluate viability and maintenance risk
before any integration decision.

## YGOPRODeck TCG Advanced secondary/backfill source

Following the PR #8 audit, `scripts/collect_ygoprodeck_lists.py` promotes
YGOPRODeck's `getDecks.php` catalogue into a **secondary/backfill** Meta
Watch source that appends new observations alongside the Konami collector
without replacing or rewriting it.

- Endpoint: **only** `https://ygoprodeck.com/api/decks/getDecks.php` with
  `_sft_category=Tournament Meta Decks`. The unavailable deck-detail API
  endpoints and any HTML scraping are explicitly out of scope.
- HTTPS certificate verification is always on (`requests` default,
  `verify=True` passed explicitly); paginated requests are paced with a
  conservative delay and cached in-memory to avoid duplicate calls.
- Only TCG Advanced records survive filtering. Any record whose format,
  deck name, tournament name, description, or excerpt hints at OCG,
  Master Duel, Rush Duel, Genesys, Duel Links, online/remote/casual/test
  markers is excluded and reported so it can never leak in as
  `TCG_ADVANCED`.
- Records are mapped into the existing observation model using only the
  catalogue fields validated by the audit: `deckNum` as the stable source
  id, `pretty_url` as the public deck URL, `format` /`tournamentName` /
  `tournamentPlacement` / `tournamentPlayerName` / `tournamentPlayerCount`
  / `submit_date` for provenance, and the comma-separated
  `main_deck` / `extra_deck` / `side_deck` arrays split into per-zone
  card entries.
- Deduplication uses `deckNum` (and the standard event/player key) both
  against the existing dataset and within the same batch, so a re-run
  never double-imports.
- Missing metadata is reported honestly (no tournament name, no player
  name, no `pretty_url`, or a relative `submit_date` like "3 days ago"
  are all recorded as rejected records rather than silently backfilled).
- If the catalogue endpoint is unreachable the existing dataset is left
  completely untouched and the failure is written into the report;
  partial runs never rewrite pre-existing entries.

Regression tests in `tests/test_ygoprodeck_lists.py` cover TCG Advanced
filtering, `deckNum`-based dedup, relative `submit_date` handling,
comma-separated deck array parsing, endpoint failure isolation, missing
metadata reporting, and format separation.

## Remaining limitations

- Only 6 lists have been imported (well below the unchanged minimum-sample
  thresholds of 20 lists / 3 events per window), so `/meta-watch` correctly
  reports "insufficient data" for all three banlist buckets -- this sample
  demonstrates the ingestion pipeline end-to-end, not a reliable adoption
  trend.
- Source pages are HTML intended for human reading (no stable decklist API),
  so parser assumptions may need maintenance if Konami changes page
  structure; parse failures are reported and never erase existing data.
- archetype and banlist_id are recorded as explicit "UNKNOWN"/"UNKNOWN-*"
  for this sample; if a future source clearly labels these, real values
  should be used instead.
- Card-name-to-printing matching is exact/prefix (plus a dash-punctuation
  normalization fallback) against `prices.db`'s `card_name` column; names
  that differ from TCGPlayer's tracked spelling in other ways will still
  show as unresolved rather than guessed.
- If/when a legitimate, licensed, or manually-curated data source becomes
  available for additional events, import it with
  `scripts/import_meta_watch_lists.py` -- do not hand-edit
  `data/meta_watch_lists.json` outside of the importer, since that bypasses
  validation and dedup.
