# PROJECT_STATUS.md

Continuation checkpoint for the combined prediction system. If you are
resuming a future session, start here.

---

## 1. Objective

Identify existing Yu-Gi-Oh TCG card printings likely to experience a
meaningful future price increase by combining:

1. New-card and archetype-support announcements.
2. Tournament adoption and emerging tech cards.
3. Sales velocity, completed sales, listings, and seller counts.
4. Reprints, set releases, banlists, rarity, and cheaper substitutes.
5. Price momentum, volatility, spreads, and liquidity.

These are complementary inputs to **one** system, not disconnected
dashboards. Announcements are allowed to generate candidates before
adoption; later inputs strengthen or weaken them.

---

## 2. Current implementation state

Branch: `copilot/get-topdeck-coverage-results` (PR #4).

Delivered in this branch — two additive, independently-reviewable
increments toward the objective:

### Increment 1 — TopDeck coverage check (live-verified)

- `scripts/check_topdeck_coverage.py`, workflow, and 8 unit tests. See
  `PROJECT_STATUS.md §3` for observed sample statistics.
- Assessment strings are scoped explicitly to the queried sample and
  never generalize beyond it (fields
  `sample_size`, `reliably_distinguished_in_sample`, and
  `assessment` values like `likely_reliable_in_sample`).

### Increment A — Unified evidence layer

- `app/evidence.py`: canonical `EvidenceRecord` (schema version 2)
  used by every future input source. Enforced invariants:

  * **Missing is unknown, not zero.** `value=None` requires an
    explicit `missing_reason`; a present value forbids one.
  * **Timestamps separated by role.** `event_time`,
    `publication_time`, and `first_seen_at` are stored distinctly.
    `event_time` is never substituted for `publication_time`.
  * **Public availability vs our observation are distinct.**
    `available_at` (derived) is the earliest time the fact was
    publicly available (research/retrospective mode) — it equals
    `publication_time` when known, else `first_seen_at`.
    `observable_at` (derived) is the earliest time *this repository
    itself* could have acted on the fact — it equals
    `first_seen_at`. **Neither timestamp uses `event_time`.**
    `event_time` is subject metadata (the future release date, the
    tournament date, the price-snapshot date); it describes what the
    record is *about* and never delays visibility. An announcement
    first seen today about a release next month is visible today in
    live simulation.
    `visible_at(mode="retrospective")` (default) uses `available_at`;
    `visible_at(mode="live_simulation")` uses `observable_at`.
    Retrospective results must always be labeled as such; a public
    availability time is not evidence that a live alert was issued.
  * **Revisions cannot leak backward.** A record with `supersedes` has
    its `available_at` additionally floored at `first_seen_at`, even
    when the revision keeps the original source's `publication_time`.
    The corrected content did not become the *record's* public value
    until we re-read the source.
  * **Card-level identity is first-class.** A support announcement can
    target a canonical card (`card_key`) and/or archetype without
    guessing a specific printing. Records use
    `mapping_uncertainty="card_level"` and may set
    `affected_printings` (list of TCGPlayer product ids currently
    believed to represent the target card). Downstream code treats
    one such record as *one confirmation* regardless of how many
    printings fan out; one announcement is never counted as several
    independent confirmations just because a card has many reprints.
  * **`product_id` (TCGPlayer id)** is the printing join key — same
    identity `prices.db` and `top_gainers.json` already use.
    `card_name`/`set_name` remain provenance hints.
    `mapping_uncertainty` values: `exact`, `resolved_by_name`,
    `ambiguous`, `unresolved`, `not_a_card`, `card_level`.
  * Append-only JSONL storage; validating reader; per-line schema
    check; malformed lines are rejected loudly rather than yielded.

- `tests/test_evidence.py`: 36 unit tests. Coverage includes the
  three PR-review regression suites — `RevisionLeakRegressionTests`,
  `ObservableAtLiveSimulationTests`, and `CardLevelIdentityTests` —
  in addition to the earlier schema, missing-vs-zero, mapping,
  timestamp, and append/iter tests.

Verified: `python -m unittest discover -s tests` → **137 tests, OK**
(99 pre-existing on `main` + 8 TopDeck coverage + 38 evidence, of
which 15 are new PR-review regressions).

Deliberately **not** in this PR (kept small for review): announcement
collector, adoption feature, combined ranking, evaluation harness,
product UI. See §6 for the sequenced follow-ups.

---

## 3. Verified data sources and coverage

| Signal | Source | Live-verified in this repo | Notes |
|---|---|---|---|
| Daily tracked price | TCGCSV daily archive | Yes, in production (`scripts/fetch_prices.py` + `daily.yml`) | Category 2 = Yu-Gi-Oh. Missing subtypes never auto-switched. |
| Momentum signal | derived from `prices.db` | Yes, in production (`scripts/early_movers.py`, `scripts/top_gainers.py`) | 15-day snapshot on 2026-09-11; live signal history in `signals.db`. |
| Meta Watch decklists (manual sample) | Konami blog HTML | Sample of 6 lists imported by hand (see `META_WATCH.md`) | Currently below the analysis' own minimum-sample threshold. |
| Automated Konami collector | Konami blog HTML | **Draft in PR #3** (`copilot/automate-meta-watch-collection`) | Not merged; belongs to another agent's branch — do not touch. |
| TopDeck tournament coverage | TopDeck v2 API | **Live-run captured (PR #4)** | Query: `game="Yu-Gi-Oh"`, `format="Advanced"`, `last=90`, generated `2026-09-15T23:34:16Z`. Sample: 70 events, 2,031 participants. Decklist availability *in this sample*: 0 with complete structured Main/Extra/Side, 1 external link, 2,030 missing. Publication timestamps present *in this sample*: 0/70. Assessment scoped to sample: `likely_reliable_in_sample` for paper-TCG-Advanced. **Scope-limited conclusion:** in this queried sample, TopDeck did not supply usable decklists for YGO Advanced; it did supply attendance data (events / participant counts). Nothing about broader time windows, different filters, or other Yu-Gi-Oh formats can be inferred from this sample alone. |
| Announcement source (YGOrganization OCG/TCG) | https://ygorganization.com/category/ocg-tcg | **Not yet integrated** | Next PR: collector + evidence emitter. |
| Sales velocity, listings, seller counts | TCGPlayer public / TCGCSV | **Not yet integrated** | Coverage unverified. Any integration must live-check coverage before feeding the ranker. |
| Banlist changes / reprint notices | Konami official | Not yet integrated | Small enough to hand-import via the evidence layer initially. |

---

## 4. Decisions

1. **TopDeck conclusions are scoped to the observed sample**, not
   generalized. The paper-distinction assessment reports
   `sample_size` and uses `*_in_sample` field names. Any future write-up
   citing TopDeck coverage must quote the query (game, format,
   lookback window, generation time) alongside the numbers.
2. **Isolated PR per increment** where the sandbox permits. This PR
   bundles two increments only because pushes from this session are
   restricted to the branch the session was cloned on; increments
   B–E each get their own branch.
3. **Missing is unknown, not zero.** Enforced in code
   (`app/evidence.py::validate_record`).
4. **Public availability vs our observation are distinct times, and
   neither uses `event_time`.** `available_at =
   publication_time or first_seen_at` (retrospective / public).
   `observable_at = first_seen_at` (live-simulation / our
   observation). `event_time` is subject metadata about *what the
   record describes* (release date, tournament date, snapshot date)
   and never enters visibility computations, so future release
   dates cannot hide announcements observed today. Callers pick
   the mode with an explicit argument to `visible_at`; the default
   is `retrospective` and callers must label such results as
   retrospective research, not as live alerts.
5. **Revisions cannot leak backward.** Enforced by flooring a
   revision's `available_at` at its own `first_seen_at`.
6. **Card-level identity is first-class.** One
   `card_level` announcement is one confirmation, regardless of
   `affected_printings` length. Unresolved-but-card-level evidence
   is preserved, not dropped.
7. **Production alerts are untouched** — Early Movers and Top
   Gainers continue to run under the existing rule from `main`.
8. **No paid APIs introduced without approval.**

---

## 5. Blockers

- **Sandbox has no outbound DNS to third-party sources.** From this
  environment: `Could not resolve host: ygorganization.com` and
  `Could not resolve host: db.ygoprodeck.com`. This prevents:
  * fetching the recent Cubic support announcement to run the
    retrospective end-to-end demonstration; and
  * validating any future announcement collector against a live
    source from this session.
- **CI network access to third-party sources is UNVERIFIED for the
  new collector.** GitHub-hosted runners generally allow outbound
  HTTPS, but reliability against `ygorganization.com`,
  `db.ygoprodeck.com`, and any other third party has not been
  demonstrated from this repository's CI. It remains unverified
  until the new `workflow_dispatch` collector job successfully
  fetches the source in a real run and uploads a sanitized
  coverage artifact.
- **`prices.db` is NOT automatically available in other jobs.** The
  daily workflow restores the snapshot in its own job via
  `scripts/restore_snapshot.py`; separate workflows do not inherit
  it. Any collector or demonstration job that needs `prices.db`
  must explicitly call `scripts/restore_snapshot.py` in its own
  steps and only proceed to price-history joins after that step
  succeeds. Fixture output must never be labeled as a live run.

---

## 5a. Cubic support retrospective demonstration — status

Attempted from this session. **Not completed.** Exact blockers:

1. `curl https://ygorganization.com/` → *Could not resolve host* (DNS
   blocked in this sandbox). The Cubic support announcement page
   could not be fetched, so no real `publication_time` or article
   text was ingested here.
2. `curl https://db.ygoprodeck.com/` → *Could not resolve host*. No
   canonical Cubic card list was available for card-level
   fan-out.
3. `data/prices.db` is absent on a fresh clone. Even with
   announcement text, TCGPlayer product-id resolution and price-history
   join could not run locally.

No fixture substitute was written, and no synthetic record was
appended to any dataset — per the task instruction to never
substitute fixtures and call the demonstration live-verified. The
demonstration is queued for the announcement-collector PR
(§6 step 2). Whether CI can actually reach the source, and whether
the collector job successfully restores `prices.db`, will only be
established once that PR's `workflow_dispatch` job runs. Until
that run succeeds, treat CI network access to
`ygorganization.com` / `db.ygoprodeck.com` and cross-job
availability of `prices.db` as unverified assumptions, not
guarantees.

---

## 6. Next actions (in order)

1. **Merge this PR** — foundational, additive, no runtime impact
   (no code imports `app/evidence.py` yet).
2. **PR #5 — Announcement collector (increment B) + Cubic
   demonstration.**
   - `scripts/collect_ygorg_announcements.py`: parse the OCG/TCG
     category index, walk into each article, extract referenced cards
     via named search/summon/recycle/support effects, cache raw HTML
     per article under `data/announcement_cache/`, and emit records
     with `evidence_type="announcement"` through `append_records()`.
     Use `mapping_uncertainty="card_level"` with `card_key`/`archetype`
     for card-scoped support; only claim printing-level (`exact`,
     `resolved_by_name`) when a specific printing is directly named.
   - `.github/workflows/announcement_collector.yml`
     (`workflow_dispatch` only) that runs the collector against the
     live source and uploads a sanitized coverage artifact
     (article count, extracted cards per article, mapping resolution
     rate).
   - Fixture-based tests for parsing, dedup, and card-level fan-out
     against a tiny `product_id` mapping fixture.
   - **Cubic demonstration:** run the collector against the Cubic
     support announcement inside the workflow, and report the exact
     card_key entries emitted, `affected_printings` after resolution
     against a snapshot-restored `prices.db`, and any price-history
     join gaps — all labeled retrospective research.
3. **PR #6 — Adoption + attendance feature (increment C, part 1).**
   - Adapter that reads existing Meta Watch JSON and TopDeck attendance
     into evidence records (`tournament_adoption`).
   - Adapter that reads `prices.db` momentum + Top Gainers cache into
     `price_momentum` / `price_snapshot` records.
4. **PR #7 — Baseline combined ranker + evaluation harness (D).**
   - `app/candidates.py`: rank existing tracked printings using
     evidence records visible at a given cutoff. Start simple:
     union of (momentum signal) and (announcement mentions) with
     transparent per-source weights and per-candidate explanations.
   - `scripts/evaluate_candidates.py`: time-ordered evaluation across
     the price history in `signals.db`, with a held-out window.
     Use `visible_at(mode="retrospective")` for research runs and
     `visible_at(mode="live_simulation")` when reporting numbers that
     will be quoted as live-alert simulation. Report top-alert
     precision, false positives, lead time, coverage, and subsequent
     returns. Include unsuccessful announcements and non-rising cards
     as controls.
   - Persist per-run candidate explanations under `data/candidate_runs/`
     without ever labeling a reconstructed historical candidate as a
     live prediction.
5. **PR #8 — Concise combined candidate view (E).**
   - Small `/candidates` route rendering: printing, evaluation time,
     why flagged, supporting/opposing evidence, current price and
     movement, data freshness, missing inputs, horizon, later outcome.

---

## 7. Relevant branches / PRs

- `main` @ `ea327b9` — production baseline; do not disturb.
- PR #3 `copilot/automate-meta-watch-collection` — Konami collector
  (another agent's branch; hands off).
- **This PR** `copilot/get-topdeck-coverage-results` (PR #4) — both
  increments above.

## 8. Test results

- `python -m unittest discover -s tests` → **137 tests, OK**
  (99 pre-existing + 8 TopDeck coverage + 38 evidence-layer, of
  which 15 are new PR-review regressions).
- No changes to production paths (`app/analysis.py`,
  `scripts/early_movers.py`, `scripts/fetch_prices.py`, snapshot
  pipeline, daily workflow) — this PR is additive.

## 9. Exact continuation task

> Continue from PROJECT_STATUS.md. Deliver PR #5 as described in
> section 6, step 2: `scripts/collect_ygorg_announcements.py` that
> emits `EvidenceRecord`s via `app.evidence.append_records`
> (using `mapping_uncertainty="card_level"` where the source is
> card-scoped), a `workflow_dispatch` workflow that captures a
> sanitized live-coverage artifact, tests against fixture HTML in
> `tests/fixtures/`, and the retrospective Cubic-support
> demonstration under CI. Do not modify production alerts. Preserve
> every invariant in section 4.
