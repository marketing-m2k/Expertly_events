# Deploying the scheduled scrape

This is a duplicate of `event-scraper/`, set up to run unattended on a
3-day schedule instead of manually. The original `event-scraper/` folder is
untouched — keep using it for one-off/manual runs and testing.

## What's different from the manual folder

- `Dockerfile` — builds the app on Microsoft's official Playwright image
  (Chromium + all OS deps already included, nothing extra to install).
- `scheduled_run.py` — runs a full scrape (`main.py --engine free`), then
  prints/emails a short digest (sites processed, events added, failures,
  which sites returned 0 events). Email is optional — see below.
- `.dockerignore` — keeps `__pycache__` and old archived output out of the
  image.

Everything else (scraper logic, master workbook, output format) is identical
to `event-scraper/`.

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
   - Schedule: every 3 days, e.g. `0 3 */3 * *` (adjust hour to your
     timezone — Coolify's cron runs in the server's timezone, typically
     UTC, same caveat as the tax-rulings scraper).
5. (Optional) To get an email digest after each run, set these environment
   variables on the Coolify application:
   - `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASS`
   - `DIGEST_TO` — who receives the summary
   - `DIGEST_FROM` — optional, defaults to `SMTP_USER`

   Without these set, `scheduled_run.py` just prints the digest to the
   container logs (visible in Coolify) instead of emailing it.

## Output

Same as the manual scraper: `output/Events.xlsx` inside the container. On
Coolify this lives inside the container's filesystem — if you want it
persisted/downloadable between runs, mount a volume at `/app/output`
(Coolify → Storage → add a persistent volume).

## Master sheet updates

The scraper reads `Tax_Legal_Finance_Events_Master.xlsx` from this same
folder. If you add/edit organizations in the manual `event-scraper/` copy,
copy the updated file into `event-scraper-automated/` too (or point this
deployment at a shared location) — the two folders don't sync
automatically.
