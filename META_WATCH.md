# Meta Watch: research-only published-list adoption tracking

Tracks whether cards are gaining adoption in **published tournament
decklists** while their tracked TCGPlayer price stays relatively flat.
This is explicitly research-only: it never labels anything a price
prediction, never recommends a purchase, and never shows a confidence
probability. It does not change Early Movers, Top Gainers/Losers, or the
production database snapshot pipeline in any way.

## Data source

Automated (unattended, non-interactive) retrieval of official Konami TCG
tournament decklist coverage was investigated for this feature. The
official Konami blog (`yugiohblog.konami.com`) *does* publish structured
"Top N Deck Lists" articles per event, but:

- In this sandboxed environment, HTTPS requests to `yugiohblog.konami.com`
  fail TLS verification (`curl: (60) SSL certificate problem: unable to
  get local issuer certificate`) with the container's default CA trust
  store. Read-only research fetches during manual investigation used
  `curl -k` to work around this locally; production/unattended import does
  **not** do this (see "Remaining limitations" below).
- There is still no documented, stable JSON/API endpoint for decklists
  (unlike TCGCSV's price API already used elsewhere in this app) -- only
  HTML articles meant for human reading.

Given that, this feature ships with a **validated JSON importer** (manual
transcription is explicitly permitted for the first sample) rather than a
live scraper, per the task's own fallback instructions.

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
- Formats/banlists are never combined: TCG Advanced, OCG, and Master Duel
  are tracked as separate `format` values, and each `banlist_id` is
  evaluated as its own pair of 14-day windows.
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
```

## Remaining limitations

- Only 6 lists have been imported (well below the unchanged minimum-sample
  thresholds of 20 lists / 3 events per window), so `/meta-watch` correctly
  reports "insufficient data" for all three banlist buckets -- this sample
  demonstrates the ingestion pipeline end-to-end, not a reliable adoption
  trend.
- The importer used for this sample was run manually against `curl -k`
  -fetched pages (see "Data source" above); an unattended/scheduled
  importer would need either a real CA bundle update for
  `yugiohblog.konami.com` or a different fetch path -- this repo does not
  currently automate that fetch at all, by design (manual transcription
  only, per the task).
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
