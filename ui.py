from datetime import datetime
from pathlib import Path

import yaml
from croniter import croniter
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_PATH = Path("/config/schedule.yaml")

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


def ensure_schedule_file():
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not SCHEDULE_PATH.exists():
        save_schedule(EMPTY_SCHEDULE)


def load_schedule():
    ensure_schedule_file()

    with SCHEDULE_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or EMPTY_SCHEDULE


def save_schedule(data: dict):
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with SCHEDULE_PATH.open("w", encoding="utf-8") as f:
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
def get_schedule():
    return load_schedule()


@app.post("/api/validate")
def validate(payload: SchedulePayload):
    errors = validate_schedule(payload.schedule)

    return {
        "valid": not errors,
        "errors": errors,
    }


@app.post("/api/schedule")
def update_schedule(payload: SchedulePayload):
    errors = validate_schedule(payload.schedule)

    if errors:
        raise HTTPException(status_code=400, detail=errors)

    save_schedule(payload.schedule)

    return {"saved": True}