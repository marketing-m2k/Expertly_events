FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
# Pin playwright to the base image's version -- a newer pip package would
# look for a Chromium build this image doesn't ship.
RUN pip install --no-cache-dir -r requirements.txt "playwright==1.47.0"

COPY . .

# Base image already ships Chromium + all OS deps for Playwright; nothing
# extra to install. Keep the container alive so Coolify's Scheduled Tasks
# feature can `exec` into it on a cron schedule (same pattern as the
# tax-rulings scraper: the schedule lives in Coolify, not in this image).
CMD ["tail", "-f", "/dev/null"]
