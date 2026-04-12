# crontinel

Crontinel monitoring SDK for Python applications. Send cron, queue, and job monitoring events from any Python app.

## Install

```bash
pip install crontinel
# or with httpx for async support:
pip install "crontinel[httpx]"
```

## Requirements

- Python 3.9+

## Quick Start

```python
from crontinel import Crontinel

client = Crontinel(
    api_key=os.environ["CRONTINEL_API_KEY"],
    app_name="my-worker",
)

# Report a cron job run
client.schedule_run(
    command="reports:generate",
    duration_ms=2340,
    exit_code=0,
)

# Report queue worker activity
client.queue_processed(
    queue="emails",
    processed=12,
    failed=0,
    duration_ms=8901,
)

# Send a custom alert
client.event(
    key="disk-space-warning",
    message="Disk usage above 90%",
    state="firing",
)
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CRONTINEL_API_KEY` | — | Your Crontinel API key (required) |
| `CRONTINEL_API_URL` | `https://app.crontinel.com` | Crontinel SaaS or self-hosted endpoint |

## `monitor_schedule` helper

Wrap any function and automatically report its outcome:

```python
result, duration_ms, exit_code = client.monitor_schedule(
    "reports:generate",
    generate_daily_reports,
)
```

## Framework integrations

### APScheduler

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from crontinel import Crontinel

client = Crontinel(api_key=os.environ["CRONTINEL_API_KEY"])
scheduler = BlockingScheduler()

@scheduler.scheduled_job("cron", hour=9, minute=0)
def daily_reports():
    result, ms, code = client.monitor_schedule("daily-reports", send_reports)
```

### Celery

```python
from crontinel import Crontinel
from celery.signals import task_success, task_failure

client = Crontinel(api_key=os.environ["CRONTINEL_API_KEY"])

@task_success.connect
def on_task_success(task=None, **kwargs):
    client.queue_processed(
        queue=task.name,
        processed=1,
        failed=0,
    )

@task_failure.connect
def on_task_failure(task=None, **kwargs):
    client.queue_processed(
        queue=task.name,
        processed=0,
        failed=1,
    )
```

## License

MIT
