# Expertly Event Scraper

Scrapes upcoming tax, legal and finance events from ~1,100 organization
websites across six countries, filters them for relevance, and builds
`output/Master.xlsx` — the verified event list used on the Expertly
events page.

| Country | Organizations |
|---|---|
| India | 208 |
| USA | 289 |
| UK | 186 |
| SG | 199 |
| UAE | 134 |
| AUS | 107 |

The organization list lives in `Sources/Event_scrapper_-_Website_completed.xlsx`,
one tab per country. That is the only file to edit when adding or removing
organizations.

## Where it runs

> **Never run this on GitHub Actions.** Scraping third-party sites from
> Actions breaks GitHub's terms — it got the `marketing-m2k` account
> suspended in Sept 2026. This repo is for code storage only; there is
> deliberately no `.github/workflows/`.

- **Now:** weekly on a local Windows PC via Task Scheduler, running
  `run_weekly.bat` (logs go to `logs/run_YYYY-MM-DD.log`).
- **Next:** a scheduled task on the M2K VPS via Coolify — see
  [DEPLOYMENT.md](DEPLOYMENT.md). Pending Admin access to Coolify.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and set `GEMINI_API_KEY` (from
aistudio.google.com/apikey). `.env` is gitignored and excluded from the
Docker image — on Coolify, set it as an environment variable instead.

## Running

```bash
# Full weekly run: every country, then rebuild Master.xlsx, then print/email a digest
python scheduled_run.py

# Same run without the digest
python weekly_full_run.py

# One country only, then merge everything into Master.xlsx afterwards
python weekly_full_run.py --country UK
python weekly_full_run.py --merge-only
```

Options for `weekly_full_run.py`:

| Flag | Effect |
|---|---|
| `--engine gemini` | Use Gemini for extraction instead of the free heuristic engine |
| `--skip-llm` | Skip the Gemini relevance-classification step during cleaning |
| `--country X` | Scrape one country (`India`, `USA`, `UK`, `SG`, `UAE`, `AUS`) |
| `--merge-only` | Skip scraping; rebuild `Master.xlsx` from existing per-country results |

For small tests, `main.py` scrapes a single tab directly, e.g.
`python main.py --source-sheet UK --limit 5`.

### Gemini cost guard

Gemini calls are capped at **$2 per Python process** (`scraper/api_budget.py`);
going over raises `BudgetExceededError` and stops the run. Because the cap
is per process, running countries one at a time with `--country` gives
each country its own $2 budget rather than one shared total.

## How it works

1. **Load** — `scraper/load_sites.py` reads each country's organizations
   and events-page URLs from the Sources workbook.
2. **Fetch** — `scraper/fetch.py` renders every events page in headless
   Chromium (Playwright) so JavaScript calendars load.
3. **Extract** — the free engine (`scraper/heuristic_extract.py`) finds
   dates in the page and pulls the surrounding title, link and location.
   The Gemini engine (`scraper/extract.py` + `scraper/gemini_extract.py`)
   asks the model for structured JSON instead.
4. **Upsert** — `scraper/excel_writer.py` writes to `output/raw/`. Every
   run re-scrapes every organization, so an event whose date or venue was
   missing last week is filled in once the site publishes it, without
   creating duplicates.
5. **Clean & classify** — `scraper/clean_events.py` dedupes, drops past
   and junk entries, verifies links (`scraper/verify_links.py`) and uses
   Gemini (`scraper/classify_relevance.py`) to filter for relevant
   events, producing each country's final workbook.
6. **Merge** — `weekly_full_run.py` combines every country's
   *Upcoming - Verified* events into `output/Master.xlsx`.

## Output

| Path | Contents |
|---|---|
| `output/Master.xlsx` | **The deliverable.** Sheets: *Verified Events*, *Needs Review* (date disagrees with the event's own text), *Stats* |
| `output/final/Events_<Country>_2026.xlsx` | Per-country review workbook: Summary, Upcoming - Verified, Upcoming - Incomplete, Past events, Flagged for Review (India's is `Events_2026.xlsx`) |
| `output/raw/Events_<Country>.xlsx` | Raw scraped rows before cleaning |
| `output/failures/` | Sites that failed to load this run (not committed) |
| `output/summaries/` | Per-country and weekly JSON summaries used for the digest |

`python -m scraper.resolve_needs_review` is an optional follow-up: it
asks Gemini to settle the real date for each *Needs Review* row in
`Master.xlsx`, moving confirmed upcoming events into *Verified Events*.

## Other tools

- `python gui.py` — desktop window for running and pausing a scrape.
- `python server.py` — local dashboard at http://localhost:8765/dashboard.html
  with start/stop controls and live progress.

## Known limitations

- Sites behind logins or CAPTCHAs, or with PDF-only calendars, fail and
  are listed in `output/failures/` for manual follow-up.
- The free engine needs dates written as plain text near the event
  title; date-picker widgets and image-only calendars return few or no
  events.
- A full run takes several hours: every site is a real browser page load.
