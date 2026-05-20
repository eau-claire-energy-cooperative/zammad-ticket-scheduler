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

            for field in ["title", "customer", "group"]:
                if not ticket.get(field):
                    errors.append(f"{ticket_label}: {field} is required.")

            if ticket.get("owner") and ticket.get("owner_id"):
                errors.append(f"{ticket_label}: use owner OR owner_id, not both.")

            article = ticket.get("article")

            if not isinstance(article, dict):
                errors.append(f"{ticket_label}: article is required.")
                continue

            for field in ["subject", "body"]:
                if not article.get(field):
                    errors.append(f"{ticket_label}: article.{field} is required.")

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