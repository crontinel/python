# Changelog

## [0.2.1] - 2026-11-16

- Fix: a plain `pip install crontinel` failed on the first call because the default client needs `requests`, which is not a dependency. It now falls back to the standard library when neither `requests` nor `httpx` is installed.
- Declare the setuptools build backend in `pyproject.toml`.
- First release on PyPI.

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
