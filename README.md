# Zammad Ticket Scheduler

A lightweight Python scheduler that creates Zammad tickets automatically on a cron-style schedule.

This project is designed for Docker use (logs to stdout, supports clean shutdown via `docker stop`, reloads config automatically). Starting with version 1.2, there is a UI built into the docker container that runs so you do not need to edit the yaml file to make changes.

---

## Features

- Cron-based scheduling (via `croniter`)
- HTML based UI served locally via python
- Reloads schedule YAML when changed (no need to restart the whole container)
- Supports multiple scheduled jobs
- Supports multiple tickets per job run
- Ticket defaults with per-ticket overrides
- Optional owner assignment (`owner` lookup or `owner_id`)
- Logs to stdout (Docker friendly)
- Optional file logging (`./tmp/scheduler.log`)
- Cleans up stale job temp files automatically
- Supports clean shutdown via `docker stop`

---

## Install

### Requirements

- Python 3.9+
- Zammad API token
- Zammad instance URL

### Install Dependencies

```bash
pip install pyyaml croniter
```

### Copy Sample Config Files

This repo includes example config files:

- `.env.example` (environment variables)
- `./config/schedule.example.yaml` (scheduler config)

Copy these into place:

- Copy `.env.example` to `.env`
- (Optional) Copy `./config/schedule.example.yaml` to `./config/schedule.yaml`
**Note:** If `schedule.yaml` does not exist in the config directory, it will be created for you on first run.

Then edit them to match your environment.

---
## Scheduler UI
In the newest update there is a UI that is hosted that allows the user to update the schedules and jobs without having to edit the physical yaml file. The default `docker-compose.yml` file maps the UI to port 18743, but if you would like to change that feel free to do so at your own comfort level. You are still welcome to use the yaml file to edit the ticket schedules if you would prefer.

### Screenshots
<img width="1530" height="717" alt="Defaults Section" src="https://github.com/user-attachments/assets/cae0cdda-9c9e-4f99-a7c9-d3e0574a40cb" />
Defaults of anything (besides timeout) can be set at the top of the screen. On initial startup, the only defaults that are created are the group (Users), article type (note), and internal article (false). 

<img width="1512" height="340" alt="No Jobs Yet" src="https://github.com/user-attachments/assets/5dd938dc-2f88-4da7-b367-96d52fb75e8c" />
By default, the `schedule.yaml` file will be created empty, but you can change that by adding a new job at the bottom of the screen after the defaults section.

<img width="1512" height="901" alt="Jobs Creation Section" src="https://github.com/user-attachments/assets/a25811c4-050d-4d56-a6d1-127614f4c50c" />
After a job is added at the bottom you can create a name and cron schedule as well as entering ticket details. More than one ticket can be added to a single cron job.


---

## Configuration

This project uses:

- **Environment variables** for Zammad authentication
- **A schedule YAML file** for job definitions

---

## Environment Variables

| Variable | Required | Description |
|---------|----------|-------------|
| `ZAMMAD_URL` | ✅ | Base URL of your Zammad instance (ex: `https://support.example.com`) |
| `ZAMMAD_TOKEN` | ✅ | Zammad API token |
| `SCHEDULER_LOG_TO_FILE` | ❌ | Set to `1` to enable `./tmp/scheduler.log` |

---

## YAML Options

### Root Keys

| Key | Type | Required |
|-----|------|----------|
| `zammad` | mapping | ❌ |
| `defaults` | mapping | ❌ |
| `jobs` | list | ✅ |

### `zammad:` Options

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `timeout` | int | ❌ | API timeout in seconds (default: `30`) |

### `jobs:` Options

Each job entry supports:

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `name` | string | ❌ | Friendly job name |
| `cron` | string | ✅ | Cron expression |
| `tickets` | list | ❌ | List of tickets to create |

---

### `tickets:` Options

Each ticket supports:

| Key | Type | Required |
|-----|------|----------|
| `title` | string | ✅ |
| `customer` | string | ✅ |
| `group` | string/int | ✅ |
| `owner` | string | ❌ |
| `owner_id` | int | ❌ |
| `article` | mapping | ✅ |
| `checklist_template_id` | int | ❌ |

> Do not set both `owner` and `owner_id`.

### `article:` Options

| Key | Type | Required |
|-----|------|----------|
| `subject` | string | ✅ |
| `body` | string | ✅ |
| `type` | string | ❌ |
| `internal` | bool | ❌ |

---

## Usage

### Run with Docker Compose

Note: this repo contains a generic `docker-compose.yml` file automatically. Feel free to adjust the file as needed for your own use, but using this docker file will start this process.

1. Copy `.env.example` to `.env` and update the values.
2. Copy `schedule.example.yaml` to `schedule.yaml` and update the schedule/jobs.
3. Start the container:

```bash
docker compose up -d
```

To view logs:

```bash
docker compose logs -f
```

---

### Run the Script(s) Manually

To run the scheduler or ticket creation script manually, you must provide the following environment variables:

- `ZAMMAD_URL`
- `ZAMMAD_TOKEN`

Note: `schedule.yaml` is the scheduler config (jobs + cron) used by `scheduler.py`
`config.yaml` is the direct ticket config format used by `zammad_create_ticket.py`

### Run the Scheduler

```bash
python scheduler.py --schedule schedule.yaml
```

The scheduler checks once per minute and runs any job that is due.

### Run Ticket Creation Manually

Run:

```bash
python zammad_create_ticket.py --config config.yaml
```

---

## Manual Config Example

Here is a minimal example config you can run manually:

```yaml
zammad:
  timeout: 30

tickets:
  - title: "Automated Test Ticket"
    customer: "customer@example.com"
    group: "Support"
    article:
      subject: "Test Ticket"
      body: "This ticket was created manually via the API script."
```

Run it by setting `ZAMMAD_URL` and `ZAMMAD_TOKEN` in your environment, then running:

```bash
python zammad_create_ticket.py --config config.yaml
```

---

## Logging

Logs always print to stdout.

To also log into a file, set the environment variable:

- `SCHEDULER_LOG_TO_FILE=1`

Logs will be written to:
```
./tmp/scheduler.log
```

---

## Exit Codes

### `zammad_create_ticket.py`

| Code | Meaning |
|------|---------|
| `0` | Success |
| `2` | Bad config / bad usage |
| `4` | Ticket creation failure |
| `5` | Network error |

---

## License

GPLv3
