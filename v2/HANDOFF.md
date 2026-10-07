# Handoff: where the v2 rebuild stands (2026-10-07)

Task: complete rebuild of the event scraper in `v2/` (see README_V2.md). v1 is untouched. **Do NOT run the full production run until the user says "RUN FULL".**

## Done
- Phase 1 baseline: 11 hand-verified orgs in `baseline/*.json` (101 true events). v1 recall 57% live / 68% ideal DOM; v2 100% / 100%.
- Phases 2-12 implemented in `pipeline/`, tests in `tests/` (205 passing locally; browser tests need `playwright install chromium`).
- Rules agreed: scraper failure never = REJECTED; official page is truth; Gemini corrections need code-verified evidence; no guessed dates/years/places.

## Open (Phase 14 integration test, then final report)
1. Needs `GEMINI_API_KEY` in the environment (not in git). Keep the $2 cap (`--gemini-budget 2`).
2. Run: `python run_v2.py --baseline-orgs --out data/integration --review all --gemini-budget 2 --workers 6`
   (run in the FOREGROUND, one command; no background watchers/timers. Gemini calls take ~15-20s each; ~125 events.)
3. Run v1 for comparison: `python tools/run_v1_pipeline.py` (writes reports/v1_pipeline_records.json), then compare v1 vs v2: discovered, missed, dups, extraction/classification differences, Gemini corrections, false rejections, unresolved, completeness, errors.
4. Findings from a partial earlier run to verify: too many NEEDS_REVIEW (fix applied: failed corrections to supplementary fields no longer block; key-field failures still do - deliberate deviation, tell the user); title-only rejection evidence now refused; ARITA event links 404 on the live site (expected FETCH_FAILED); TEI "Season Pass" rows were rejected by Gemini - ask the user whether those count as events.
5. Re-run `python run_v2.py --reprocess --out data/integration` after code changes (uses saved pages).
6. Write reports/PHASE1_BASELINE_REPORT.md, then the final summary the user asked for (implementation summary, files created/modified/retired, test + integration results, recall, limitations, risks, exact full-run command, what the full QC report contains). Then STOP and wait for "RUN FULL".

## Lessons
- Never put regexes with backslashes through shell heredocs (they get mangled: `\b` -> backspace). Use file-edit tools.
- Don't start sleep/timer background tasks; check logs directly.
- `baseline/raw/` (captured pages) is gitignored; re-capture with `tools/capture_live.py <slug> <url>` if needed. `python -m measure.run --engine v2 --mode live` works without it.
