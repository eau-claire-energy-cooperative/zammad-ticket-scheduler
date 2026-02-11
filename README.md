# Zammad Ticket Scheduler

A lightweight Python scheduler that creates Zammad tickets automatically on a cron-style schedule.

This project is designed for Docker use (logs to stdout, supports clean shutdown via `docker stop`, reloads config automatically).

---

## Features

- Cron-based scheduling (via `croniter`)
- Reloads schedule YAML when changed (no need to restart the whole container)
- Supports multiple scheduled jobs
- Supports multiple tickets per job run
- Ticket defaults with per-ticket overrides
- Optional owner assignment (`owner` lookup or `owner_id`)
- Logs to stdout (Docker friendly)
- Optional file logging (`./tmp/scheduler.log`)
- Cleans up stale job temp files automatically
- Supports clean shutdown via `docker stop` (SIGTERM)

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
- `schedule.example.yaml` (scheduler config)

Copy these into place:

- Copy `.env.example` to `.env`
- Copy `schedule.example.yaml` to `schedule.yaml`

Then edit them to match your environment.

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

### Run the Scheduler

```bash
python scheduler.py --schedule schedule.yaml
```

The scheduler checks once per minute and runs any job that is due.

---

### Run Ticket Creation Manually

To run the ticket creation script manually, you must provide the following environment variables:

- `ZAMMAD_URL`
- `ZAMMAD_TOKEN`

Then run:

```bash
python zammad_create_ticket.py --config config.yaml
```

---

Note: `schedule.yaml` is the scheduler config (jobs + cron).  
`config.yaml` is the direct ticket config format used by `zammad_create_ticket.py`.

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

MIT
