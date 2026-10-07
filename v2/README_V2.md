# Event scraper v2

A rebuild of the event pipeline around one rule: **every discovered event must be accounted for at every stage, and a scraper failure is never a rejection.**
v1 (the parent folder) is untouched and remains the fallback. v2 writes only inside its own `--out` folder.

## Pipeline

```
SOURCE -> DISCOVERY -> COMPLETENESS CHECK -> DETAIL EXTRACTION -> QUALITY CHECK -> FIELD RE-SCRAPE
       -> SOURCE VERIFICATION -> CLASSIFICATION SIGNALS -> GEMINI REVIEW -> EVIDENCE VALIDATION
       -> FINAL STATUS -> STORE / MASTER -> QC REPORT
```

| Stage | Module |
|---|---|
| Discovery: cards (no sibling-repetition assumption), JSON-LD, RSS/ICS/sitemap, pagination, Load more, infinite scroll, browser | `pipeline/discovery/` (`cards.py`, `jsonld.py`, `feeds.py`, `pagination.py`, `browser.py`, `orchestrator.py`) |
| Discovery completeness (`discovery_complete / incomplete / failed`) | `pipeline/discovery/completeness.py` |
| Detail extraction (scoped to the event, label/value pairs kept) | `pipeline/extraction/` (`page_context.py`, `pairs.py`, `fields.py`, `detail.py`) |
| Quality flags | `pipeline/quality.py` |
| Field-level re-scrape ladder | `pipeline/rescrape.py` |
| Source verification + source-of-truth packet | `pipeline/verify.py` |
| Classification signals (Tax/Finance/Legal; `scraper/taxonomy.json` is editable) | `pipeline/classify.py` |
| Gemini review + evidence validation | `pipeline/gemini_review.py`, `pipeline/llm.py` |
| Final status | `pipeline/status.py` |
| Store, history, dedup, Master | `pipeline/store.py`, `pipeline/identity.py`, `pipeline/master.py` |
| QC / audit | `pipeline/qc.py` |
| Reprocessing / versions | `pipeline/reprocess.py`, `pipeline/models.py` (versions) |
| Orchestration | `pipeline/run.py`, `run_v2.py` |
| Measurement harness | `measure/`, `baseline/` |

## Statuses

`DISCOVERED, EXTRACTED, QUALITY_REVIEW, NEEDS_AI_REVIEW, NEEDS_REVIEW, VERIFIED, CORRECTED, REJECTED, PAST, DISCOVERY_INCOMPLETE (organisation level), FETCH_FAILED`.
`NEEDS_REVIEW` is the one addition to the brief's list: the state after Gemini has looked and a person must decide (the brief's Gemini output also has NEEDS_REVIEW).
`REJECTED` is reachable **only** through a Gemini rejection whose evidence quote the code found in the event's own page, whose page was readable, whose category is outside Tax/Finance/Legal and whose title has no strong Tax/Finance/Legal topic. Fetch failures, missing dates, unreadable pages, uncertainty and an exhausted API budget can never produce it.

## Commands

```bash
python -m pytest tests                                   # 200+ tests (browser tests use a local server)
python -m measure.run --engine v1 --mode live --out reports/baseline_v1_live
python -m measure.run --engine v2 --mode live --out reports/v2_live
python run_v2.py --baseline-orgs --out data/integration --review all        # small integration run (11 organisations)
python run_v2.py --reprocess --out data/integration                         # re-run records affected by a version change (uses saved pages)
python run_v2.py --countries India USA UK SG UAE AUS --review all --gemini-budget <usd> --out data/run_full --confirm-full "RUN FULL"   # refuses without the phrase
```

## Versions and reprocessing

`pipeline/models.py` holds `PIPELINE_VERSION`, `EXTRACTOR_VERSION`, `CLASSIFIER_VERSION`, `REVIEW_PROMPT_VERSION`. Bump the extractor version after changing extraction code and `--reprocess` will re-extract every
affected record from the raw page copy saved at the time (no network), re-verify it, and reconsider previously REJECTED / NEEDS_REVIEW records. History (status, field and correction history) is kept.

## Outputs (inside `--out`)

`store.sqlite` (events, status/correction/field history, ledger, org runs), `evidence/` (gzipped copies of every official page read), `Master_v2.xlsx`, `qc_report.md/json`, `discovery_results.json`.
