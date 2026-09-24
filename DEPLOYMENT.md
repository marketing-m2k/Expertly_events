# Deploying the scheduled scrape

This is a duplicate of `event-scraper/`, set up to run unattended on a
3-day schedule instead of manually. The original `event-scraper/` folder is
untouched — keep using it for one-off/manual runs and testing.

## What's different from the manual folder

- `Dockerfile` — builds the app on Microsoft's official Playwright image
  (Chromium + all OS deps already included, nothing extra to install).
- `weekly_full_run.py` — does a FULL re-scrape of every organization in both
  the India and USA tabs of `Sources/Event_scrapper_-_Website_completed.xlsx`
  (not an incremental resume), then runs the cleaning/classification step
  for each into `output/Events_2026.xlsx` and `output/Events_USA_2026.xlsx`.
  A full re-scrape (rather than only appending new finds) is what lets a
  previously-incomplete event's date get filled in once the source site
  finally publishes it — see `scraper/excel_writer.py`'s `upsert_events`.
- `scheduled_run.py` — runs `weekly_full_run.py`, then prints/emails a short
  digest per country (orgs checked, verified/incomplete/past/flagged
  counts, failures). Email is optional — see below.
- `.dockerignore` — keeps `__pycache__` and old archived output out of the
  image.

Everything else (scraper logic, output format) is identical to
`event-scraper/`. Note: the master org list is `Sources/Event_scrapper_-_Website_completed.xlsx`
(India + USA tabs) — that's the only source file to edit going forward.

## Build

```bash
cd event-scraper-automated
docker build -t expertly-event-scraper .
```

## Deploy to Coolify

1. Push this folder to a repo (or a subfolder of one) Coolify can pull from.
2. In Coolify, create a new **Application** from that repo, Dockerfile build
   pack, pointing at this folder.
3. Deploy it — the container starts and just idles (`tail -f /dev/null`),
   waiting for scheduled runs. It does not scrape on its own on startup.
4. Add a **Scheduled Task** on the application:
   - Command: `python3 scheduled_run.py`
   - Schedule: weekly, e.g. `0 3 * * 1` (every Monday at 3am — adjust hour
     to your timezone; Coolify's cron runs in the server's timezone,
     typically UTC).
   - A full weekly re-scan of ~440 orgs across both countries takes a while
     (each site is a real headless-browser page load) — make sure the
     Scheduled Task's timeout, if Coolify has one configured, is generous
     enough that it isn't killed mid-run.
5. (Optional) To get an email digest after each run, set these environment
   variables on the Coolify application:
   - `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASS`
   - `DIGEST_TO` — who receives the summary
   - `DIGEST_FROM` — optional, defaults to `SMTP_USER`

   Without these set, `scheduled_run.py` just prints the digest to the
   container logs (visible in Coolify) instead of emailing it.

## Output

`output/` is organized into subfolders, one country's worth of files each run:
- `output/raw/Events.xlsx` / `Events_<Country>.xlsx` — raw scraped rows
  (intermediate; every organization's events before cleaning/classification).
- `output/final/Events_2026.xlsx` / `Events_<Country>_2026.xlsx` — the final
  reviewed workbooks, each with 5 sheets: Summary, Upcoming - Verified,
  Upcoming - Incomplete, Past events, Flagged for Review.
- `output/failures/failures.csv` / `failures_<Country>.csv` — per-country
  sites that failed to load this run.
- `output/summaries/summary_<Country>.json` / `weekly_summary.json` —
  machine-readable run summaries used to build the email digest.
- `output/Master.xlsx` — the final cross-country deliverable (kept at the
  `output/` root, not a subfolder, since it's the one file meant for actual
  downstream use).
- `output/progress.json`, `output/dashboard.html` — live operational files
  read by `main.py`/`server.py` by a fixed path; kept at the `output/` root.

On Coolify this all lives inside the container's filesystem — mount a
volume at `/app/output` (Coolify → Storage → add a persistent volume) so
it's downloadable between runs, and so a full re-scrape can reconcile
against last week's raw data (fill in a date that was previously missing)
instead of starting from empty every time.

## Master sheet updates

The scraper reads `Sources/Event_scrapper_-_Website_completed.xlsx` (India
and USA tabs) from this same folder — that is the single source of truth
for which organizations get scraped. Edit it directly in this repo; there
is no separate master file to keep in sync anymore.
