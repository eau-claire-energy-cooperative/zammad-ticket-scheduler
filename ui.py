from datetime import datetime
from pathlib import Path

import yaml
from croniter import croniter
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path("/config")
EXCLUDED_SCHEDULE_FILES = {"schedule.example.yaml"}

HISTORY_DIR = CONFIG_DIR / "history"
MAX_HISTORY_FILES = 3

EMPTY_SCHEDULE = {
    "zammad": {"timeout": 30},
    "defaults": {
        "group": "Users",
        "article": {
            "type": "note",
            "internal": False,
        },
    },
    "jobs": [],
}

app = FastAPI(title="Zammad Ticket Scheduler UI")

app.mount(
    "/styles",
    StaticFiles(directory=BASE_DIR / "styles"),
    name="styles",
)


class SchedulePayload(BaseModel):
    schedule: dict


def list_schedule_files() -> list[str]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    return sorted(
        p.name for p in CONFIG_DIR.glob("*.yaml")
        if p.is_file() and p.name not in EXCLUDED_SCHEDULE_FILES
    )


def get_schedule_path(filename: str | None = None) -> Path:
    files = list_schedule_files()

    if filename is None:
        if files:
            filename = files[0]
        else:
            filename = "schedule.yaml"

    if filename not in files and filename != "schedule.yaml":
        raise HTTPException(status_code=400, detail="Invalid schedule file.")

    return CONFIG_DIR / filename


def ensure_schedule_file(filename: str | None = None) -> Path:
    path = get_schedule_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        save_schedule(EMPTY_SCHEDULE, path.name)

    return path


def load_schedule(filename: str | None = None):
    path = ensure_schedule_file(filename)

    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or EMPTY_SCHEDULE

def backup_schedule_file(path: Path) -> None:
    if not path.exists():
        return

    history_subdir = HISTORY_DIR / path.stem
    history_subdir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = history_subdir / f"{path.stem}.{timestamp}.yaml"

    backup_path.write_text(
        path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    backups = sorted(
        history_subdir.glob("*.yaml"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )

    for old_backup in backups[MAX_HISTORY_FILES:]:
        old_backup.unlink()

def save_schedule(data: dict, filename: str | None = None):
    path = get_schedule_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def get_default(data: dict, field: str):
    defaults = data.get("defaults") or {}
    return defaults.get(field)


def get_default_article(data: dict, field: str):
    defaults = data.get("defaults") or {}
    article = defaults.get("article") or {}
    return article.get(field)


def validate_schedule(data: dict):
    errors = []

    if not isinstance(data, dict):
        return ["Schedule must be a YAML object."]

    jobs = data.get("jobs")

    if jobs is None:
        errors.append("jobs is required.")
        return errors

    if not isinstance(jobs, list):
        errors.append("jobs must be a list.")
        return errors

    default_title = get_default(data, "title")
    default_customer = get_default(data, "customer")
    default_group = get_default(data, "group")
    default_subject = get_default_article(data, "subject")
    default_body = get_default_article(data, "body")

    for job_index, job in enumerate(jobs):
        job_label = f"Job {job_index + 1}"

        if not isinstance(job, dict):
            errors.append(f"{job_label}: must be an object.")
            continue

        cron = job.get("cron")

        if not cron:
            errors.append(f"{job_label}: cron is required.")
        else:
            try:
                croniter(str(cron), datetime.now())
            except Exception as e:
                errors.append(f"{job_label}: invalid cron '{cron}' — {e}")

        tickets = job.get("tickets", [])

        if not isinstance(tickets, list):
            errors.append(f"{job_label}: tickets must be a list.")
            continue

        for ticket_index, ticket in enumerate(tickets):
            ticket_label = f"{job_label}, Ticket {ticket_index + 1}"

            if not isinstance(ticket, dict):
                errors.append(f"{ticket_label}: must be an object.")
                continue

            article = ticket.get("article") or {}

            if not ticket.get("title") and not default_title:
                errors.append(f"{ticket_label}: title is required unless set in defaults.")

            if not ticket.get("customer") and not default_customer:
                errors.append(f"{ticket_label}: customer is required unless set in defaults.")

            if not ticket.get("group") and not default_group:
                errors.append(f"{ticket_label}: group is required unless set in defaults.")

            if ticket.get("owner") and ticket.get("owner_id"):
                errors.append(f"{ticket_label}: use owner OR owner_id, not both.")

            if ticket.get("state") and ticket.get("state_id"):
                errors.append(f"{ticket_label}: use state OR state_id, not both.")

            if ticket.get("priority") and ticket.get("priority_id"):
                errors.append(f"{ticket_label}: use priority OR priority_id, not both.")

            if not article.get("subject") and not default_subject:
                errors.append(f"{ticket_label}: article.subject is required unless set in defaults.")

            if not article.get("body") and not default_body:
                errors.append(f"{ticket_label}: article.body is required unless set in defaults.")

    return errors


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "ui.html").read_text(encoding="utf-8")


@app.get("/api/schedule")
def get_schedule(file: str | None = None):
    return load_schedule(file)

@app.get("/api/schedules")
def get_schedule_files():
    files = list_schedule_files()

    if not files:
        save_schedule(EMPTY_SCHEDULE, "schedule.yaml")
        files = list_schedule_files()

    return {"files": files}


@app.post("/api/validate")
def validate(payload: SchedulePayload):
    errors = validate_schedule(payload.schedule)

    return {
        "valid": not errors,
        "errors": errors,
    }


@app.post("/api/schedule")
def update_schedule(payload: SchedulePayload, file: str | None = None):
    errors = validate_schedule(payload.schedule)

    if errors:
        raise HTTPException(status_code=400, detail=errors)

    path = get_schedule_path(file)

    backup_schedule_file(path)
    save_schedule(payload.schedule, file)

    return {"saved": True, "file": file}