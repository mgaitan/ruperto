# ruperto

[![CI](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml/badge.svg)](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml)
[![docs](https://img.shields.io/badge/docs-blue.svg?style=flat)](https://mgaitan.github.io/ruperto/)
[![pypi version](https://img.shields.io/pypi/v/ruperto.svg)](https://pypi.org/project/ruperto/)
[![Changelog](https://img.shields.io/github/v/release/mgaitan/ruperto?include_prereleases&label=changelog)](https://github.com/mgaitan/ruperto/releases)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/mgaitan/ruperto/blob/main/LICENSE)

Conversational ordering backend for food businesses.

The current MVP direction is a transactional assistant focused on:

- customer identification by channel identity, starting with phone number,
- menu and order guidance,
- Spanish (Argentina)-first customer and staff interactions,
- future-ready channel adapters, beginning with WhatsApp via Kapso,
- a small admin surface for active orders, customers, menu, and store settings.

## Quick Start

Install dependencies and initialize the local database:

```bash
uv sync
uv run ruperto init-db
```

Run the API locally:

```bash
uv run fastapi dev
```

Run the development web chat UI:

```bash
uv run ruperto web-chat
```

You should then have:

- API root at `http://127.0.0.1:8000/`
- health check at `http://127.0.0.1:8000/healthz`
- store profile at `http://127.0.0.1:8000/api/store-profile`
- menu listing at `http://127.0.0.1:8000/api/menu-items`
- development chat endpoint at `http://127.0.0.1:8000/api/dev/messages`
- development web chat at `http://127.0.0.1:7932/`

## Development

- Install dependencies with `uv sync`.
- Initialize the local database with `uv run ruperto init-db`.
- Run the API locally with `make serve` or `uv run fastapi dev`.
- New dependency releases are delayed by one week via `uv` cooldown (`[tool.uv].exclude-newer = "1 week"`), with per-package overrides when required (for example, `ty`).
- Install [`prek`](https://github.com/j178/prek) as an external tool:

```bash
uv tool install prek
```

- Install git hooks with `prek`:

```bash
prek install
```

- Run the local QA bundle with `prek`:

```bash
prek run --all-files
```

- PRs with documentation changes publish a docs preview at:

```text
https://mgaitan.github.io/ruperto/_preview/pr-<PR_NUMBER>/
```

## Documentation

- Docs follow [Diataxis](https://diataxis.fr/).
- Start at `docs/index.md` and read:
  - `docs/getting_started.md` (tutorial),
  - `docs/development_workflow.md` (how-to),
  - `docs/configuration.md` (reference),
  - `docs/about_the_docs.md` (explanation and design rationale).
