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

Create the first dashboard admin interactively:

```bash
uv run ruperto create-admin
```

Run the API locally:

```bash
uv run fastapi dev
```

For automated or non-interactive setups, you can still bootstrap one owner user through environment variables:

```bash
export RUPERTO_DASHBOARD_SESSION_SECRET="replace-this-in-production"
export RUPERTO_DASHBOARD_ADMIN_EMAIL="staff@example.com"
export RUPERTO_DASHBOARD_ADMIN_PASSWORD="change-me"
export RUPERTO_DASHBOARD_ADMIN_NAME="Store Admin"
```

If you plan to send transactional email from the same backend, the SMTP
settings are now also recognized through `RUPERTO_SMTP_SERVER`,
`RUPERTO_SMTP_PORT`, `RUPERTO_SMTP_USER`, and `RUPERTO_SMTP_PASSWORD`.

The bootstrap store can also start on another business vertical through
`RUPERTO_STORE_VERTICAL`. The current accepted values are `ordering`
and `municipal`, with `ordering` as the default.
You can also set `RUPERTO_STORE_SLUG` to control the public tenant slug used
by the embedded browser demo.

For Kapso-backed WhatsApp, configure:

- `RUPERTO_KAPSO_API_KEY`
- `RUPERTO_KAPSO_PHONE_NUMBER_ID`
- `RUPERTO_KAPSO_WEBHOOK_SECRET`

Run the development web chat UI:

```bash
uv run ruperto web-chat
```

Or open the browser demo page served by the main app:

- `http://127.0.0.1:8000/demo/chat/<tenant-slug>`

This page is intentionally simple and talks to `/api/dev/messages/<tenant-slug>`
directly.
It lets you switch between multiple demo phone numbers so you can emulate
returning customers with remembered context inside one tenant.

You should then have:

- API root at `http://127.0.0.1:8000/`
- health check at `http://127.0.0.1:8000/healthz`
- browser demo chat at `http://127.0.0.1:8000/demo/chat/<tenant-slug>`
- staff dashboard at `http://127.0.0.1:8000/dashboard`
- store profile at `http://127.0.0.1:8000/api/store-profile`
- menu listing at `http://127.0.0.1:8000/api/menu-items`
- development chat endpoint at `http://127.0.0.1:8000/api/dev/messages`
- Kapso WhatsApp webhook at `http://127.0.0.1:8000/webhooks/whatsapp/kapso`
- development web chat at `http://127.0.0.1:7932/`

The current development flow no longer blocks purely informational questions
behind the customer's name. The assistant can answer menu or delivery
questions first, then ask for the name once it is actually needed to keep
building or confirming the order. If the first customer message already
contains an order, that intent is still remembered and resumed as soon as the
customer shares their name, instead of resetting the conversation. The
assistant also estimates kitchen delay from preparation time plus active
workload and lets staff move orders through operational statuses with
`PATCH /api/orders/{order_id}/status`.
Store opening hours are now configurable through `GET/PUT /api/store-hours`.
Each weekday can have zero, one, or many opening slots, so a store can stay
closed on Mondays, open only at lunch on Sundays, or split the day into as
many service windows as needed. Customer replies mention the next opening time
whenever the store is closed.
If a customer asks for a later ready time such as `para las 12`, the backend
can now keep the order scheduled for that slot, store when preparation should
start, and include the configured transfer alias when the payment method is
`transferencia`.
After the checkout details are complete, the assistant now shows a deterministic
review of the persisted draft and only closes the order after an explicit final
confirmation from the customer. Informational delivery questions such as
shipping cost or area coverage also stay in informational mode instead of
forcing the checkout script.
The backend also serves a simple Tailwind staff dashboard at `/dashboard`.
Dashboard access now uses a minimal session cookie login backed by a bootstrap
staff user configured through environment variables. Once signed in, the team
gets a split navigation with a home page for metrics and recent orders, a
dedicated customers page, and separate settings pages for the menu, store
profile, agent behavior, flexible weekly hours, and store memberships.
The demo catalog now includes a broader synthetic menu with pizzas,
hamburgers, lomitos, milanesas, wraps, empanadas, drinks, and desserts so
local development can exercise more realistic ordering conversations and
simple add-on suggestions.
Informational menu questions are also handled more proactively now: if a
customer asks for a category such as soft drinks, the assistant should list
concrete options with prices instead of answering with a bare yes/no.
That browsing layer now also respects simple constraints such as `sin alcohol`
when suggesting drinks, asks for clarification instead of over-interpreting
ambiguous follow-ups like `uno de cada` across multiple product groups, and
can recover some large first-turn multi-item orders deterministically if the
model fails before building the draft.
Compact customer messages are also handled more naturally now, so the assistant
can reuse cues such as a self-introduction, a payment hint like `te pago acá`,
and a same-turn price question without asking for the same detail twice.
The service also starts carrying an explicit `default_store_id` setting as the
first groundwork toward a logical multi-tenant deployment, while still running
today as one configurable store by default. Dashboard users can already belong
to more than one store and switch the active store for profile and opening-hour
management, while orders, customers, and the catalog still use the shared MVP
data model for now.
Each tenant now carries a fixed `vertical`, so different tenants can route the
shared channel/core infrastructure to different assistant domains without
changing identity from the dashboard later.
The municipal vertical now routes the shared chat endpoint to a dedicated
intake assistant that guides neighbors through area selection, category
selection, request description, location capture, and final confirmation
without changing the rotisería behavior.
Each tenant also has a public `slug`, which is used by the embedded browser
demo and keeps routes such as `/demo/chat/mi-muni` and `/demo/chat/mi-roti`
bound to the right tenant.
Municipal tenants now also have a first domain model of their own: service
areas, optional categories, and cases with status, assignment, and precise
location fields. Local development bootstraps a demo municipal catalog with
areas such as public lighting, street maintenance, water requests, and urban
hygiene so the intake flow has realistic data to work with from day one.
Each municipal category can also be marked as either a complaint or a request,
so the assistant can phrase confirmation and completion messages with the right
operational nuance for cases such as water delivery.
The municipal intake also keeps a few guardrails in the chat layer: it asks
for a more precise location before submitting a case, nudges the citizen to
rephrase if the message contains insults, and uses the person's first name in
the final confirmation to keep the tone concise and natural.
When the same citizen comes back later, the municipal assistant can now also
answer proactive follow-up questions such as "¿cómo va mi caso?" or "¿qué pasó
con mi caso #12?" without forcing a brand-new intake flow first.
There is now also a first production-shaped WhatsApp integration path through
Kapso: the backend can receive inbound text messages from the
`/webhooks/whatsapp/kapso` endpoint, answer through the Kapso proxy, and send
automatic ready/almost-ready/out-for-delivery notifications when a WhatsApp
order changes status. The adapter is intentionally isolated behind a channel
layer so future providers or channels do not leak into the assistant logic.
The ordering assistant can also answer proactive follow-up questions such as
"¿cómo va mi pedido?" from stored order state, even before a new inbound
notification is sent.
Kapso credentials can still come from environment variables as a development
fallback, but the recommended path for multi-store setups is now to configure
each local's WhatsApp connection from the dashboard agent settings page.
If the assistant decides a conversation should be delegated, the dashboard
customers page now exposes that handoff queue and lets staff answer through the
same store-owned WhatsApp connection before returning control to the bot.

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
