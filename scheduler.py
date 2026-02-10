#!/usr/bin/env python3
"""
scheduler.py

Cross-platform scheduler that:
- Loads a schedule YAML file (reloads when it changes)
- Checks jobs every minute
- When a job is due, writes a per-job temp config YAML into ./tmp/
- Invokes zammad_create_ticket.main(["--config", <temp_yaml>])
- Logs output to ./tmp/scheduler.log
- Supports clean shutdown via SIGTERM / Ctrl+C / stop file

Stop file:
    Create ./tmp/STOP_SCHEDULER to exit cleanly
"""

from __future__ import annotations

import time
import re
import sys
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
    """
    Signal handler that triggers a clean shutdown.

    Used for Ctrl+C (SIGINT) and SIGTERM (common in Docker/Kubernetes).
    """
    global SHOULD_EXIT
    SHOULD_EXIT = True


def safe_filename(name: str) -> str:
    """
    Convert a string into a filesystem-safe filename fragment.

    Used to prevent unsafe characters in temp YAML filenames.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    return cleaned or "unnamed"


def get_project_tmp_dir() -> Path:
    """
    Ensure ./tmp exists next to this script and return it.

    This is used for:
      - scheduler.log
      - STOP_SCHEDULER file
      - per-job generated YAML files
    """
    tmp_dir = Path(__file__).resolve().parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def setup_logging(log_file: Path) -> logging.Logger:
    """
    Configure file-based logging (idempotent).

    Uses a dedicated logger name so imports do not interfere with root logging.
    """
    logger = logging.getLogger("scheduler")
    logger.setLevel(logging.INFO)

    # Avoid duplicate log handlers if module is imported/reloaded
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Write logs to disk for persistence
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


def load_schedule(path: Path) -> dict:
    """
    Load and validate the schedule YAML file.

    Requires:
      - root mapping
      - jobs list
    """
    if not path.exists():
        raise FileNotFoundError(f"Schedule YAML not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if not isinstance(cfg, dict):
        raise ValueError("Schedule config must be a mapping/object.")
    if "jobs" not in cfg or not isinstance(cfg["jobs"], list):
        raise ValueError("Schedule config must have jobs: list")

    return cfg


def file_fingerprint(p: Path) -> tuple[int, int]:
    """
    Return a cheap fingerprint for change detection: (mtime_ns, size).

    This is lightweight and avoids re-reading YAML unless needed.
    """
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


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
    stop_file = tmp_dir / "STOP_SCHEDULER"

    logger = setup_logging(log_file)

    logger.info("Scheduler started. Checking every minute.")
    logger.info(f"Schedule file: {schedule_path}")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Stop file: {stop_file}")

    # Initial config load (fail fast if invalid)
    cfg = load_schedule(schedule_path)
    last_fp = file_fingerprint(schedule_path)

    # last_run tracks the previous time window for cron checking
    last_run = datetime.now()

    while not SHOULD_EXIT:
        now = datetime.now()

        # Allow clean shutdown by dropping a stop file into ./tmp
        if stop_file.exists():
            logger.info("Stop file detected. Shutting down scheduler cleanly.")
            break

        # Reload schedule if changed since last load
        try:
            fp = file_fingerprint(schedule_path)
            if fp != last_fp:
                cfg = load_schedule(schedule_path)
                last_fp = fp
                logger.info("Schedule file changed; reloaded config.")
        except Exception as e:
            # Keep running using last known good config
            logger.error(f"Failed to reload schedule file: {e}", exc_info=True)

        # Extract shared config sections (optional)
        defaults = cfg.get("defaults", {}) or {}
        zammad = cfg.get("zammad", {}) or {}

        # Process each job independently
        for job in cfg["jobs"]:
            if not isinstance(job, dict):
                logger.warning("Job entry is not a mapping/object; skipping.")
                continue

            cron = job.get("cron")
            name = job.get("name", "<unnamed>")

            # Skip jobs without a cron expression
            if not cron or not isinstance(cron, str):
                logger.warning(f"Job '{name}' missing cron, skipping.")
                continue

            # Check if job should run within this time window
            try:
                due = is_due(cron, last_run, now)
            except Exception as e:
                logger.error(f"Job '{name}' has invalid cron '{cron}': {e}")
                continue

            if not due:
                continue

            # Write a per-job config file so ticket creator can run normally
            safe_name = safe_filename(name)
            tmp_file = tmp_dir / f"sched_{safe_name}.yaml"

            run_cfg = {
                "zammad": zammad,
                "defaults": defaults,
                "tickets": job.get("tickets", []),
            }

            try:
                # Write temporary YAML file for this job execution
                with tmp_file.open("w", encoding="utf-8") as fh:
                    yaml.safe_dump(run_cfg, fh, sort_keys=False)

                logger.info(f"Job started: {name}")

                # Run ticket creation (returns an exit code)
                rc = int(create_main(["--config", str(tmp_file)]))

                if rc == 0:
                    logger.info(f"Job completed successfully: {name}")
                else:
                    logger.error(f"Job completed with errors: {name} (exit code {rc})")

            except Exception as e:
                # Capture traceback for debugging
                logger.error(f"Job FAILED: {name} | Error: {e}", exc_info=True)

        # Update last_run so cron window advances correctly
        last_run = now

        # Sleep until the next minute boundary to keep cron checks aligned
        next_minute = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
        sleep_for = (next_minute - datetime.now()).total_seconds()
        if sleep_for < 0:
            sleep_for = 0
        time.sleep(sleep_for)

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    # Handle clean shutdown (Ctrl+C / Docker stop / kill)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    # SIGTERM doesn't exist on some Windows setups, so guard it
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)

    import argparse

    # CLI entrypoint expects schedule YAML file path
    p = argparse.ArgumentParser()
    p.add_argument("--schedule", required=True, help="Path to schedule YAML")
    args = p.parse_args()

    try:
        run_scheduler(args.schedule)
    except Exception as e:
        # Logging may not exist yet, so fallback to stderr
        print(f"Scheduler crashed: {e}", file=sys.stderr)
        raise
