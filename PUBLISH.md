# Publishing crontinel to PyPI

## Prerequisites
1. Create account at https://pypi.org (GitHub OAuth login)
2. Enable 2FA on PyPI account
3. Generate an API token at https://pypi.org/manage/account/#api-tokens
4. Add token to `~/.pypirc`:

```
[pypi]
username = __token__
password = pypi-xxxxyour-token-xxxx
```

## Publish

```bash
cd python/
python3 -m pip install build twine
python3 -m build
python3 -m twine upload dist/*
```

## After publish

```bash
python3 -m pip install crontinel  # verify install
```

## Notes
- Package name on PyPI: `crontinel`
- Current version: 0.1.0
- Update version with: `bump2version patch` or manually edit `pyproject.toml`
