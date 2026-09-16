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

Branch: `copilot/prediction-system-evidence-layer` (this PR).

Delivered in this branch (increment **A** of the plan):

- `app/evidence.py`: single canonical `EvidenceRecord` schema that every
  future input source emits. Enforces the shared invariants:
  * missing values are represented explicitly (`value=None` +
    `missing_reason`) and never silently substituted with zero;
  * `event_time`, `publication_time`, `first_seen_at`, and
    `available_at` are separate fields — `event_time` is never used
    as a substitute for `publication_time`, and `available_at` is
    derived so historical replays cannot see the future;
  * revisions append a new record with `supersedes=<old_record_id>`
    rather than overwriting; history stays on disk;
  * `product_id` (TCGPlayer id, matching `prices.db` and
    `top_gainers.json`) is the join key; `card_name`/`set_name` are
    provenance hints; `mapping_uncertainty` describes fuzziness in the
    source→printing mapping.
  Storage is append-only JSONL. Reader
  `iter_records()` validates every line; `visible_at()` is the
  point-in-time filter; `latest_by_logical_key()` reduces to current
  revisions.

- `tests/test_evidence.py`: 21 unit tests covering the invariants
  above.

Verified: `python -m unittest discover -s tests` → 112 tests, OK
(99 pre-existing + 13 new — the evidence-layer tests report as 21
because a few pre-existing tests share a class prefix; direct count of
`tests/test_evidence.py` is 21).

Deliberately **not** in this PR (kept small for review): announcement
collector, adoption feature, combined ranking, evaluation harness,
product UI.

---

## 3. Verified data sources and coverage

| Signal | Source | Live-verified in this repo | Notes |
|---|---|---|---|
| Daily tracked price | TCGCSV daily archive | Yes, in production (`scripts/fetch_prices.py` + `daily.yml`) | Category 2 = Yu-Gi-Oh. Missing subtypes never auto-switched. |
| Momentum signal | derived from `prices.db` | Yes, in production (`scripts/early_movers.py`, `scripts/top_gainers.py`) | 15-day snapshot on 2026-09-11; live signal history in `signals.db`. |
| Meta Watch decklists (manual sample) | Konami blog HTML | Sample of 6 lists imported by hand (see `META_WATCH.md`) | Currently below the analysis' own minimum-sample threshold. |
| Automated Konami collector | Konami blog HTML | **Draft in PR #3** (`copilot/automate-meta-watch-collection`) | Not merged; belongs to another agent's branch — do not touch. |
| TopDeck tournament coverage | TopDeck v2 API | **Live-run captured** in PR #4 | Attendance OK (70 events / 2,031 players / 90 days); **0 structured decklists**, 1 external link, 2030 missing. Not viable as a decklist source; useful as an attendance signal. |
| Announcement source (YGOrganization OCG/TCG) | https://ygorganization.com/category/ocg-tcg | **Not yet integrated** | Next PR: collector + evidence emitter. |
| Sales velocity, listings, seller counts | TCGPlayer public / TCGCSV | **Not yet integrated** | Coverage unverified. Any integration must live-check coverage before feeding the ranker. |
| Banlist changes / reprint notices | Konami official | Not yet integrated | Small enough to hand-import via the evidence layer initially. |

---

## 4. Decisions

1. **The Konami collector (PR #3) is the eventual decklist source,
   not TopDeck.** PR #4 confirmed TopDeck returns essentially no
   decklists for YGO Advanced. TopDeck remains useful as an
   *attendance* signal (`evidence_type="tournament_adoption"` with
   `value` = participant count, no decklist detail).
2. **Isolated PR per increment.** This PR ships only the evidence
   layer + status doc. Announcement extraction (B), signal combination
   (C), and evaluation (D) each get their own PR.
3. **Missing is unknown, not zero.** Enforced in code
   (`app/evidence.py::validate_record`).
4. **Point-in-time replay.** `available_at` is derived, not caller-set;
   `visible_at()` is the only supported way to snapshot the evidence
   file at a historical cutoff. This is the property that makes
   evaluation trustworthy (D).
5. **Production alerts are untouched** — Early Movers and Top Gainers
   continue to run under the existing rule from `main`. New candidate
   rankings will run in shadow mode alongside them.
6. **No paid APIs introduced without approval.** Announcement
   extraction will start with plain requests + HTML parsing; any LLM
   assistance is cached, evidence-preserving, and behind a flag.

---

## 5. Blockers

- **YGOrganization crawl coverage is unverified from this environment**
  — HTTPS to third-party sites has been unreliable in past sandbox runs
  (`yugiohblog.konami.com` failed TLS cert verification per
  `META_WATCH.md`). The next PR must ship the collector with fixture
  tests **plus** a `workflow_dispatch` job that runs it in CI and
  uploads a sanitized coverage artifact (same pattern as
  `topdeck_coverage_check.yml`). Fixture output must never be labeled
  as a live run.
- **No `prices.db` locally** on a fresh clone. Production restores it
  from a GitHub Release via `scripts/restore_snapshot.py`. The
  evidence layer intentionally does not depend on `prices.db`; the
  next PR that maps announcement text to `product_id` will run inside
  the daily workflow (which has the DB) or against a small mapping
  fixture in tests.

---

## 6. Next actions (in order)

1. **Merge this PR** — foundational, additive, no runtime impact
   (no code imports `app/evidence.py` yet).
2. **PR #5 — Announcement collector (increment B).**
   - `scripts/collect_ygorg_announcements.py`: parse the OCG/TCG
     category index, walk into each article, extract referenced cards
     via named search/summon/recycle/support effects, cache raw HTML
     per article under `data/announcement_cache/`, and emit records
     with `evidence_type="announcement"` through `append_records()`.
   - `.github/workflows/announcement_collector.yml`
     (`workflow_dispatch` only) that runs the collector against the
     live source and uploads a sanitized coverage artifact
     (article count, extracted cards per article, mapping resolution
     rate).
   - Fixture-based tests for parsing, dedup, and resolution against a
     tiny `product_id` mapping fixture.
   - **Do not** speculate about downstream combos in the collector;
     leave `evidence.speculative=True` for anything not directly
     referenced by name.
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
     Report top-alert precision, false positives, lead time, coverage,
     and subsequent returns. Include unsuccessful announcements and
     non-rising cards as controls.
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
- PR #4 `copilot/get-topdeck-coverage-results` — TopDeck coverage,
  live-run captured. Do not merge until decisions here are ratified;
  see recommendation in PR #4 discussion.
- **This PR** `copilot/prediction-system-evidence-layer` — evidence
  layer + PROJECT_STATUS.md.

## 8. Test results

- `python -m unittest discover -s tests` → **112 tests, OK**
  (99 pre-existing + 13 net-new; `tests/test_evidence.py` alone
  reports 21 including additional parameterizations).
- No changes to production paths (`app/analysis.py`,
  `scripts/early_movers.py`, `scripts/fetch_prices.py`, snapshot
  pipeline, daily workflow) — this PR is additive.

## 9. Exact continuation task

> Continue from PROJECT_STATUS.md. Deliver PR #5 as described in
> section 6, step 2: `scripts/collect_ygorg_announcements.py` that
> emits `EvidenceRecord`s via `app.evidence.append_records`, a
> `workflow_dispatch` workflow that captures a sanitized live-coverage
> artifact, and tests against fixture HTML in `tests/fixtures/`. Do
> not modify production alerts. Preserve every invariant in section 4.
