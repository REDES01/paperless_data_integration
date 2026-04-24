"""
Behavior emulator — asyncio supervisor.

Runs correction_bot and search_bot concurrently. Every minute, prints a
summary of what each bot has done since startup (makes Grafana/log
scraping trivial without needing Prometheus pushgateway).

Failure model: if a bot raises (which shouldn't happen — they each have
their own try/except at the tick level), the supervisor logs and
restarts it after 10s. System stays alive as long as at least one bot
can make progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time

import correction_bot
import search_bot
from config import CORRECTIONS_PER_MIN, MODE, SEARCHES_PER_MIN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-15s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


async def _supervised(name: str, coro_factory, stats) -> None:
    """Run a bot coroutine; if it crashes, log + restart after backoff."""
    while True:
        try:
            await coro_factory(stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[%s] crashed: %s — restarting in 10s", name, exc)
            await asyncio.sleep(10)


async def _status_printer(
    correction_stats: correction_bot.Stats,
    search_stats:     search_bot.Stats,
) -> None:
    """Emit a JSON status line every 60s. Structured so it can be
    grep'd / tail'd without parsing free-form log lines."""
    started = time.time()
    while True:
        await asyncio.sleep(60)
        status = {
            "ts":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uptime_s":     int(time.time() - started),
            "mode":         MODE,
            "corrections": {
                "attempted": correction_stats.attempted,
                "inserted":  correction_stats.inserted,
                "skipped":   correction_stats.skipped,
                "errors":    correction_stats.errors,
            },
            "searches": {
                "searches":          search_stats.searches,
                "results_inspected": search_stats.results_inspected,
                "feedback_sent":     search_stats.feedback_sent,
                "by_action":         search_stats.by_action,
                "errors":            search_stats.errors,
            },
        }
        print("[STATUS]", json.dumps(status), flush=True)


async def main() -> None:
    log.info("behavior_emulator starting — MODE=%s", MODE)
    log.info("rates: corrections=%.2f/min, searches=%.2f/min",
             CORRECTIONS_PER_MIN, SEARCHES_PER_MIN)

    correction_stats = correction_bot.Stats()
    search_stats     = search_bot.Stats()

    tasks = [
        asyncio.create_task(_supervised("correction_bot", correction_bot.run, correction_stats)),
        asyncio.create_task(_supervised("search_bot",     search_bot.run,     search_stats)),
        asyncio.create_task(_status_printer(correction_stats, search_stats)),
    ]

    # Clean shutdown on SIGTERM (docker stop sends SIGTERM, then SIGKILL
    # after 10s grace)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in tasks])

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("shutdown signal received")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
