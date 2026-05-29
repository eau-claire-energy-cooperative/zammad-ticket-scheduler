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
    """Signal handler for Ctrl+C (SIGINT) and SIGTERM (Docker)."""
    global SHOULD_EXIT
    SHOULD_EXIT = True


def safe_filename(name: str) -> str:
    """Convert a string into a filesystem-safe filename fragment."""
    name = (name or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return cleaned or "unnamed"

# Edit this list to any config files you want to exclude
EXCLUDED_CONFIG_FILES = {"schedule.example.yaml"}

def discover_schedule_files(config_dir: Path) -> list[Path]:
    return sorted(
        p for p in config_dir.glob("*.yaml")
        if p.is_file() and p.name not in EXCLUDED_CONFIG_FILES
    )

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

def expected_tmp_files(configs: dict[Path, dict], tmp_dir: Path) -> set[Path]:
    out: set[Path] = set()

    for schedule_path, cfg in configs.items():
        jobs = cfg.get("jobs", []) or []

        for job in jobs:
            if not isinstance(job, dict):
                continue

            name = job.get("name", "unnamed")
            safe_name = safe_filename(f"{schedule_path.stem}_{name}")
            out.add(tmp_dir / f"sched_{safe_name}.yaml")

    return out


def prune_stale_tmp_files(configs: dict[Path, dict], tmp_dir: Path, logger: logging.Logger) -> None:
    keep = expected_tmp_files(configs, tmp_dir)
    existing = sorted(tmp_dir.glob("sched_*.yaml"))

    logger.info(f"PRUNE: tmp_dir={tmp_dir} keep={len(keep)} existing={len(existing)}")

    for p in existing:
        if p not in keep:
            try:
                p.unlink()
                logger.info(f"DELETE: {p.name}")
            except Exception as e:
                logger.warning(f"DELETE FAILED: {p.name} | {e}")

def load_all_schedules(config_dir: Path) -> dict[Path, dict]:
    configs: dict[Path, dict] = {}

    for schedule_path in discover_schedule_files(config_dir):
        configs[schedule_path] = load_schedule(schedule_path)

    return configs

def run_scheduler(config_dir_str: str) -> None:
    global SHOULD_EXIT

    config_dir = Path(config_dir_str).resolve()
    tmp_dir = get_project_tmp_dir()
    log_file = tmp_dir / "scheduler.log"
    logger = setup_logging(log_file)

    logger.info("Scheduler started. Checking every minute.")
    logger.info(f"Config directory: {config_dir}")

    if not config_dir.exists():
        raise FileNotFoundError(f"Config directory not found: {config_dir}")

    configs = load_all_schedules(config_dir)
    last_mtimes = {
        path: schedule_mtime(path)
        for path in discover_schedule_files(config_dir)
    }

    prune_stale_tmp_files(configs, tmp_dir, logger)

    last_run = datetime.now()

    while not SHOULD_EXIT:
        now = datetime.now()

        try:
            current_files = discover_schedule_files(config_dir)
            current_mtimes = {
                path: schedule_mtime(path)
                for path in current_files
            }

            if current_mtimes != last_mtimes:
                configs = load_all_schedules(config_dir)
                last_mtimes = current_mtimes
                logger.info("Schedule config files changed; reloaded.")
                prune_stale_tmp_files(configs, tmp_dir, logger)

        except Exception as e:
            logger.error(f"Failed to reload schedule files: {e}", exc_info=True)

        for schedule_path, cfg in configs.items():
            jobs = cfg.get("jobs", []) or []
            defaults = cfg.get("defaults", {}) or {}
            zammad = cfg.get("zammad", {}) or {}

            for job in jobs:
                if SHOULD_EXIT:
                    break

                if not isinstance(job, dict):
                    continue

                name = job.get("name", "unnamed")
                cron = job.get("cron")

                if not cron:
                    logger.warning(f"{schedule_path.name}: Job '{name}' missing cron; skipping.")
                    continue

                try:
                    if not is_due(str(cron), last_run, now):
                        continue
                except Exception as e:
                    logger.error(f"{schedule_path.name}: Job '{name}' invalid cron '{cron}': {e}")
                    continue

                safe_name = safe_filename(f"{schedule_path.stem}_{name}")
                tmp_file = tmp_dir / f"sched_{safe_name}.yaml"

                run_cfg = {
                    "zammad": zammad,
                    "defaults": defaults,
                    "tickets": job.get("tickets", []) or [],
                }

                try:
                    with tmp_file.open("w", encoding="utf-8") as fh:
                        yaml.safe_dump(run_cfg, fh, sort_keys=False)

                    logger.info(f"Job started: {schedule_path.name} / {name}")
                    rc = int(create_main(["--config", str(tmp_file)]))

                    if rc == 0:
                        logger.info(f"Job completed: {schedule_path.name} / {name}")
                    else:
                        logger.error(f"Job completed with errors: {schedule_path.name} / {name} (exit code {rc})")

                except Exception as e:
                    logger.error(f"Job FAILED: {schedule_path.name} / {name} | Error: {e}", exc_info=True)

        last_run = now

        next_minute = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
        sleep_for = max(0, (next_minute - datetime.now()).total_seconds())
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
    p.add_argument("--config-dir", default="/config", help="Directory containing schedule YAML files")
    args = p.parse_args()

    try:
        run_scheduler(args.config_dir)
    except Exception as e:
        print(f"Scheduler crashed: {e}", file=sys.stderr)
        raise
