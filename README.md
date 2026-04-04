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
- staff dashboard at `http://127.0.0.1:8000/dashboard`
- store profile at `http://127.0.0.1:8000/api/store-profile`
- menu listing at `http://127.0.0.1:8000/api/menu-items`
- development chat endpoint at `http://127.0.0.1:8000/api/dev/messages`
- development web chat at `http://127.0.0.1:7932/`

The current development flow asks for the customer's name before the first
order unless the customer already introduced themself in the opening message.
If the first customer message already contains an order or menu question, that
intent is now remembered and resumed as soon as the customer shares their
name, instead of resetting the conversation. The assistant also estimates
kitchen delay from preparation time plus active workload and lets staff move
orders through operational statuses with `PATCH /api/orders/{order_id}/status`.
Store opening hours are now configurable through `GET/PUT /api/store-hours`,
and customer replies mention the next opening time whenever the store is closed.
If a customer asks for a later ready time such as `para las 12`, the backend
can now keep the order scheduled for that slot, store when preparation should
start, and include the configured transfer alias when the payment method is
`transferencia`.
The backend also serves a simple Tailwind staff dashboard at `/dashboard` so
the team can review recent orders, adjust order statuses, and edit store and
bot settings without calling the API by hand.
The demo catalog now includes a broader synthetic menu with pizzas,
hamburgers, lomitos, milanesas, wraps, empanadas, drinks, and desserts so
local development can exercise more realistic ordering conversations and
simple add-on suggestions.
Compact customer messages are also handled more naturally now, so the assistant
can reuse cues such as a self-introduction, a payment hint like `te pago acá`,
and a same-turn price question without asking for the same detail twice.
The service also starts carrying an explicit `default_store_id` setting as the
first groundwork toward a logical multi-tenant deployment, while still running
today as one configurable store by default.

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
