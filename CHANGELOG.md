# Changelog

## v0.1.0 (2026-04-13)

First release on PyPI.

### Added
- `crontinel` Python package — Python SDK for Crontinel API
- `Crontinel` client class
- `schedule_run` — report scheduled command outcome
- `queue_processed` — report queue worker activity
- `event` — send custom events and alerts
- `monitor_schedule` — decorator/wrapper for cron jobs
- Framework integrations: APScheduler, Celery
- MIT license

## Installation

```bash
pip install crontinel
pip install "crontinel[httpx]"  # with async support
```
