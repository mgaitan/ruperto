# Getting Started (Tutorial)

This tutorial gives you a complete first pass through local setup, database
bootstrap, API startup, checks, and docs.

## 1. Create the environment

From the project root:

```bash
uv sync
```

This resolves dependencies and creates the local virtual environment.

## 2. Initialize the local database

Bootstrap the SQLite database and the first store profile:

```bash
uv run ruperto init-db
```

You can inspect the effective non-secret settings with:

```bash
uv run ruperto show-settings
```

## 3. Run the API locally

```bash
uv run fastapi dev src/ruperto/app.py
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/healthz`

When invoking modules directly from source, set {term}`PYTHONPATH` so imports resolve cleanly:

```bash
PYTHONPATH=src uv run -m ruperto --help
```

## 4. Run quality checks

```bash
make qa
make test
```

If `prek` is installed, `make qa` runs the local QA bundle with hooks.

## 5. Build the documentation

```bash
make docs
```

To open generated HTML:

```bash
make docs-open
```
