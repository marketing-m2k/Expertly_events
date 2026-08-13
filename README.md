# Expertly Event Scraper

Visits each organization's events page (from `Tax_Legal_Finance_Events_Master_100_Organizations.xlsx`),
extracts upcoming events, and writes them to an Excel sheet in the same
format shown on the Expertly events page: Date, Category, Format
(In Person/Virtual), Event Name, Description, Location, Country,
Organizer, Register Link.

## Two extraction engines

| | `--engine free` (default) | `--engine gemini` |
|---|---|---|
| Cost | $0, no API key needed | ~$0.02–0.10 per full run (Gemini 2.0 Flash) |
| How it works | Regex date-matching + DOM structure heuristics | LLM reads the page and returns structured JSON |
| Accuracy | Good on typical "event card" layouts, misses unusual ones, more false positives/negatives | Handles varied/messy layouts much better |
| Needs | Nothing extra | `GEMINI_API_KEY` env var |

Start with `free` — it costs nothing and works fine on standard listing
pages. If too many of the 286 sites come back empty or wrong in
`output/Events.xlsx`, switch that batch to `--engine gemini`.

## How it works

1. `scraper/load_sites.py` reads the 286 organizations + events-page URLs
   from the master workbook.
2. `scraper/fetch.py` renders each events page with headless Chromium
   (Playwright), so JS-driven calendars load correctly.
3. **Free engine**: `scraper/heuristic_extract.py` scans the rendered HTML
   for date-like text, walks up to the surrounding card, and pulls a
   title/link/location out of it with regex and DOM heuristics — no API
   call at all.
   **Gemini engine**: `scraper/extract.py` strips the page to readable
   text, then `scraper/gemini_extract.py` sends it to Gemini with a fixed
   JSON schema for structured extraction.
4. `scraper/excel_writer.py` appends new events to `output/Events.xlsx`,
   skipping ones already recorded (same event name + date + organizer).

## Setup

```bash
cd event-scraper
pip install -r requirements.txt
playwright install chromium
```

Only if you plan to use `--engine gemini`, set a Gemini API key (free at
aistudio.google.com/apikey):

```bash
# PowerShell
$env:GEMINI_API_KEY = "AIza..."

# bash
export GEMINI_API_KEY="AIza..."
```

## Run

Test on a handful of sites first (free, no setup needed beyond `pip install`):

```bash
python main.py --limit 5
```

Check `output/Events.xlsx` and `output/failures.csv`, then run the rest:

```bash
python main.py --start 5
```

Re-running is safe — it skips events already in the sheet and only adds
new ones. To use Gemini instead, add `--engine gemini` to either command.

## Known limitations

- Sites that require login, heavy CAPTCHAs, or PDF-only calendars will
  land in `output/failures.csv` for manual follow-up regardless of engine.
- **Free engine**: relies on dates appearing as plain text near an event
  title in the HTML. Sites using date-picker widgets, images-as-text, or
  heavily nested/obfuscated markup will return few or no events. Expect
  to spot-check results and possibly re-run problem sites with
  `--engine gemini`.
- **Gemini engine**: infers missing years/formats from context;
  spot-check a sample of rows before trusting the sheet fully.
- Both engines are rate-limited (1s between sites) to be polite to
  target sites, so a full 286-site run takes a while either way.
