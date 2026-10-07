"""Site health check: compares each website's result this run with last
run's, so a broken, blocked, emptied or redesigned site is noticed the week
it happens instead of silently producing nothing."""

import json
import os

BIG_DROP_RATIO = 0.5
MIN_PREVIOUS_FOR_DROP = 6  # a site that only ever had 2-3 events swings too easily to judge


def assess_sites(current: dict, previous: dict) -> dict:
    """current/previous: {source_url: {"organizer", "events", "error"}}.
    Returns {source_url: {"status": ok|failed|empty|big_drop, "detail": str}}."""
    health = {}
    for url, now in current.items():
        before = (previous.get(url) or {}).get("events", 0)
        if now.get("error"):
            health[url] = {"status": "failed", "detail": f"did not load: {now['error'][:120]}"}
        elif now["events"] == 0 and before > 0:
            health[url] = {"status": "empty", "detail": f"0 events (last run: {before})"}
        elif before >= MIN_PREVIOUS_FOR_DROP and now["events"] < before * BIG_DROP_RATIO:
            health[url] = {"status": "big_drop", "detail": f"{now['events']} events (last run: {before})"}
        else:
            health[url] = {"status": "ok", "detail": f"{now['events']} events"}
    return health


def healthy_sources(health: dict) -> set[str]:
    return {url for url, h in health.items() if h["status"] == "ok"}


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
