#!/usr/bin/env python3
"""
scheduler.py

Cross-platform scheduler that:
- Loads a schedule YAML file
- Checks jobs every minute
- When a job is due, writes a per-job temp config YAML into ./tmp/
- Invokes zammad_create_ticket.main(["--config", <temp_yaml>])
- Logs output to ./tmp/scheduler.log
- Supports clean shutdown via SIGTERM / Ctrl+C / stop file

Stop file:
    Create ./tmp/STOP_SCHEDULER to exit cleanly
"""

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


# Global flag for clean shutdown
SHOULD_EXIT = False


def handle_shutdown_signal(signum, frame):
    global SHOULD_EXIT
    SHOULD_EXIT = True


def safe_filename(name: str) -> str:
    """
    Make a filesystem-safe filename fragment.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "unnamed"


def get_project_tmp_dir() -> Path:
    """
    Use ./tmp next to this script.
    """
    project_dir = Path(__file__).resolve().parent
    tmp_dir = project_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def setup_logging(log_file: Path) -> logging.Logger:
    """
    Configure file-based logging.
    """
    logger = logging.getLogger("scheduler")
    logger.setLevel(logging.INFO)

    # Prevent duplicate handlers if something imports this
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    return logger


def load_schedule(path: str) -> dict:
    schedule_path = Path(path)
    if not schedule_path.exists():
        raise FileNotFoundError(f"Schedule YAML not found: {schedule_path}")

    with schedule_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("Schedule config must be a mapping/object.")
    if "jobs" not in cfg or not isinstance(cfg["jobs"], list):
        raise ValueError("Schedule config must have jobs: list")

    return cfg


def is_due(cron_expr: str, last_check: datetime, now: datetime) -> bool:
    """
    Return True if the cron schedule has a time between last_check and now.
    """
    itr = croniter(cron_expr, last_check)
    next_time = itr.get_next(datetime)
    return last_check < next_time <= now


def run_scheduler(config_path: str) -> None:
    global SHOULD_EXIT

    tmp_dir = get_project_tmp_dir()
    log_file = tmp_dir / "scheduler.log"
    stop_file = tmp_dir / "STOP_SCHEDULER"

    logger = setup_logging(log_file)

    cfg = load_schedule(config_path)
    defaults = cfg.get("defaults", {})
    zammad = cfg.get("zammad", {})

    logger.info("Scheduler started. Checking every minute.")
    logger.info(f"Schedule file: {config_path}")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Stop file: {stop_file}")

    last_run = datetime.now()

    while not SHOULD_EXIT:
        now = datetime.now()

        # Allow clean stop by file drop
        if stop_file.exists():
            logger.info("Stop file detected. Shutting down scheduler cleanly.")
            break

        for job in cfg["jobs"]:
            cron = job.get("cron")
            name = job.get("name", "<unnamed>")

            if not cron:
                logger.warning(f"Job '{name}' missing cron, skipping.")
                continue

            if is_due(cron, last_run, now):
                safe_name = safe_filename(name)
                tmp_file = tmp_dir / f"sched_{safe_name}.yaml"

                run_cfg = {
                    "zammad": zammad,
                    "defaults": defaults,
                    "tickets": job.get("tickets", []),
                }

                try:
                    # Write temp YAML
                    with tmp_file.open("w", encoding="utf-8") as fh:
                        yaml.safe_dump(run_cfg, fh, sort_keys=False)

                    logger.info(f"Job started: {name}")

                    # Run ticket creator
                    create_main(["--config", str(tmp_file)])

                    logger.info(f"Job completed successfully: {name}")

                except Exception as e:
                    logger.error(f"Job FAILED: {name} | Error: {e}", exc_info=True)

        last_run = now

        # Sleep until next minute boundary
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

    p = argparse.ArgumentParser()
    p.add_argument("--schedule", required=True, help="Path to schedule YAML")
    args = p.parse_args()

    try:
        run_scheduler(args.schedule)
    except Exception as e:
        # Log setup might not exist yet, so fallback to stderr
        print(f"Scheduler crashed: {e}", file=sys.stderr)
        raise
