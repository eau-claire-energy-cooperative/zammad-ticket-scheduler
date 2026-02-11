#!/usr/bin/env python3
"""
scheduler.py

Simple cross-platform scheduler that:
- Loads a schedule YAML file (reloads when it changes)
- Checks jobs every minute
- When a job is due, writes a per-job temp config YAML into ./tmp/
- Invokes zammad_create_ticket.main(["--config", <temp_yaml>])
- Logs to stdout (so it shows in `docker logs`)
- Optionally logs to ./tmp/scheduler.log as well
- Supports clean shutdown via SIGTERM / Ctrl+C

Docker stop:
    `docker stop` sends SIGTERM (then SIGKILL after its timeout),
    so this exits cleanly as long as the loop checks SHOULD_EXIT.
"""

from __future__ import annotations

import os
import re
import sys
import time
import signal
import logging
from pathlib import Path
from datetime import datetime, timedelta

import yaml
from croniter import croniter

from zammad_create_ticket import main as create_main


# Global shutdown flag set by signal handlers
SHOULD_EXIT = False


def handle_shutdown_signal(signum, frame) -> None:
    """Signal handler for Ctrl+C (SIGINT) and SIGTERM (Docker/Kubernetes)."""
    global SHOULD_EXIT
    SHOULD_EXIT = True


def safe_filename(name: str) -> str:
    """Convert a string into a filesystem-safe filename fragment."""
    name = (name or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return cleaned or "unnamed"


def get_project_tmp_dir() -> Path:
    """Ensure ./tmp exists next to this script and return it."""
    tmp_dir = Path(__file__).resolve().parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def setup_logging(log_file: Path) -> logging.Logger:
    """
    Configure logging to stdout (Docker-friendly).
    Optionally also logs to a file if SCHEDULER_LOG_TO_FILE=1.
    """
    logger = logging.getLogger("scheduler")
    logger.setLevel(logging.INFO)

    # Prevent duplicate handlers if code is re-imported
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always log to stdout so `docker logs` shows everything
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # Optional file logging (handy if you also want persistence)
    if os.getenv("SCHEDULER_LOG_TO_FILE", "").strip() == "1":
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def load_schedule(path: Path) -> dict:
    """
    Load schedule YAML.
    Kept intentionally simple: assumes YAML returns a dict with "jobs".
    """
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def schedule_mtime(path: Path) -> int:
    """Simple change detection: mtime in nanoseconds."""
    return path.stat().st_mtime_ns


def is_due(cron_expr: str, last_check: datetime, now: datetime) -> bool:
    """
    Return True if cron schedule triggers between last_check and now.
    This avoids missing executions if the loop drifts slightly.
    """
    itr = croniter(cron_expr, last_check)
    next_time = itr.get_next(datetime)
    return last_check < next_time <= now


def run_scheduler(schedule_path_str: str) -> None:
    """
    Main scheduler loop.

    - Reloads schedule YAML if it changes
    - Runs due jobs and calls zammad_create_ticket
    - Logs each job execution and exit code
    """
    global SHOULD_EXIT

    schedule_path = Path(schedule_path_str).resolve()
    tmp_dir = get_project_tmp_dir()
    log_file = tmp_dir / "scheduler.log"

    logger = setup_logging(log_file)

    logger.info("Scheduler started. Checking every minute.")
    logger.info(f"Schedule file: {schedule_path}")
    if os.getenv("SCHEDULER_LOG_TO_FILE", "").strip() == "1":
        logger.info("File logging: ON")
    else:
        logger.info("File logging: OFF (SCHEDULER_LOG_TO_FILE=0)")

    if not schedule_path.exists():
        raise FileNotFoundError(f"Schedule YAML not found: {schedule_path}")

    cfg = load_schedule(schedule_path)
    last_mtime = schedule_mtime(schedule_path)

    # Window tracking for cron checks
    last_run = datetime.now()

    while not SHOULD_EXIT:
        now = datetime.now()

        # Reload schedule if file changed
        try:
            mtime = schedule_mtime(schedule_path)
            if mtime != last_mtime:
                cfg = load_schedule(schedule_path)
                last_mtime = mtime
                logger.info("Schedule file changed; reloaded.")
        except Exception as e:
            # Keep running using last known config
            logger.error(f"Failed to reload schedule file: {e}", exc_info=True)

        jobs = cfg.get("jobs", []) or []
        defaults = cfg.get("defaults", {}) or {}
        zammad = cfg.get("zammad", {}) or {}

        # Run due jobs
        for job in jobs:
            if SHOULD_EXIT:
                break

            if not isinstance(job, dict):
                continue

            name = job.get("name", "unnamed")
            cron = job.get("cron")

            if not cron:
                logger.warning(f"Job '{name}' missing cron; skipping.")
                continue

            try:
                if not is_due(str(cron), last_run, now):
                    continue
            except Exception as e:
                logger.error(f"Job '{name}' invalid cron '{cron}': {e}")
                continue

            safe_name = safe_filename(name)
            tmp_file = tmp_dir / f"sched_{safe_name}.yaml"

            run_cfg = {
                "zammad": zammad,
                "defaults": defaults,
                "tickets": job.get("tickets", []) or [],
            }

            try:
                with tmp_file.open("w", encoding="utf-8") as fh:
                    yaml.safe_dump(run_cfg, fh, sort_keys=False)

                logger.info(f"Job started: {name}")
                rc = int(create_main(["--config", str(tmp_file)]))

                if rc == 0:
                    logger.info(f"Job completed: {name}")
                else:
                    logger.error(f"Job completed with errors: {name} (exit code {rc})")

            except Exception as e:
                logger.error(f"Job FAILED: {name} | Error: {e}", exc_info=True)

        # Advance cron window
        last_run = now

        # Sleep until next minute boundary (wake up and check SHOULD_EXIT again)
        next_minute = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
        sleep_for = max(0, (next_minute - datetime.now()).total_seconds())

        # Sleep in small chunks so SIGTERM exits promptly even if we're "sleeping"
        # (Signals set SHOULD_EXIT; chunking just makes responsiveness obvious.)
        end = time.time() + sleep_for
        while not SHOULD_EXIT and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)

    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--schedule", required=True, help="Path to schedule YAML")
    args = p.parse_args()

    try:
        run_scheduler(args.schedule)
    except Exception as e:
        print(f"Scheduler crashed: {e}", file=sys.stderr)
        raise
