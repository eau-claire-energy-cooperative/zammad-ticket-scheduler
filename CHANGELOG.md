# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## 2.1

### Added

- Line on the UI to reference the README for more details

### Changed

- The user interface files to accept multiple config files
- Updated the python `scheduler.py` file to accept multiple configs at once
- Scheduler python file also now purges old jobs it does not need

## 2.0

### Added

- Components (`styles/ui.css`, `ui.html`, and `ui.py`) for a web based user interface
- Automatic creation of a `schedule.yaml` file if one does not exist

### Changed

- Docker components to host the new UI
- The `requirements.txt` list to account for the additional UI requirements
- The path of the config files so they are listed in their own directory

## 1.1

### Changed

- Python `zammad_create_ticket.py` now supports an optional `checklist_templat_id` field
- README now outlines how to use this field

## 1.0

### Added

- Python `zammad_create_ticket.py` and `schedule.py` files to create and schedule tickets
- Required `Dockerfile` and `docker-compose.yml` files to spin up the docker container
- Generic `schdule.example.yaml` and `.env.example` file examples
