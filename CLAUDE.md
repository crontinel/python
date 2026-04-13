# crontinel Python — CLAUDE.md

Python SDK for Crontinel cron and queue monitoring.

## Package
- PyPI: `crontinel` (NOT yet published — need PyPI account + `python3 -m build && twine upload`)
- GitHub: `github.com/crontinel/python`

## Stack
- Python 3.9+, httpx (for async)
- `pyproject.toml` for packaging

## Key files
- `src/crontinel/__init__.py` — main SDK
- `tests/` — pytest tests
- `pyproject.toml` — project config

## Commands
```bash
pip install -e ".[httpx]"   # dev install with async
pip install -e ".[dev]"      # dev install with test deps
pytest                       # run tests
```

## Publish (manual)
```bash
python3 -m pip install build twine
python3 -m build
python3 -m twine upload dist/*
```

See `PUBLISH.md` for full PyPI setup instructions.

## Env vars
- `CRONTINEL_API_KEY` — API key (from app.crontinel.com/settings)
- `CRONTINEL_API_URL` — API URL (default: https://app.crontinel.com)
