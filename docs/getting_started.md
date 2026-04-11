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

If you want to use the staff dashboard, create the first admin interactively:

```bash
uv run ruperto create-admin
```

For non-interactive environments, you can still bootstrap a first owner
through environment variables:

```bash
export RUPERTO_DASHBOARD_SESSION_SECRET="replace-this-in-production"
export RUPERTO_DASHBOARD_ADMIN_EMAIL="staff@example.com"
export RUPERTO_DASHBOARD_ADMIN_PASSWORD="change-me"
export RUPERTO_DASHBOARD_ADMIN_NAME="Store Admin"
```

If this instance will later send transactional email, you can also define the
SMTP variables already supported by the configuration layer:

```bash
export RUPERTO_SMTP_SERVER="smtp.example.com"
export RUPERTO_SMTP_PORT="587"
export RUPERTO_SMTP_USER="mailer@example.com"
export RUPERTO_SMTP_PASSWORD="change-me"
```

If you want to test the first Kapso-backed WhatsApp channel, also configure:

```bash
export RUPERTO_KAPSO_API_KEY="change-me"
export RUPERTO_KAPSO_PHONE_NUMBER_ID="123456789012345"
export RUPERTO_KAPSO_WEBHOOK_SECRET="change-me"
```

You can inspect the effective non-secret settings with:

```bash
uv run ruperto show-settings
```

The bootstrap profile defaults to an `es-AR` locale so the MVP can keep
customer and staff-facing interactions in Spanish (Argentina) from the start.
It also respects {term}`RUPERTO_DEFAULT_STORE_ID`, which is the first step
toward a future logical multi-tenant deployment while keeping a single store
active by default today.
If you want to bootstrap a different tenant domain from the start, you can
also set {term}`RUPERTO_STORE_VERTICAL` to `municipal`. Otherwise the default
remains `ordering`.
You can also set {term}`RUPERTO_STORE_SLUG` if you want a predictable public
slug for tenant-specific demo routes.

## 3. Run the API locally

```bash
uv run fastapi dev
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/healthz`
- `http://127.0.0.1:8000/dashboard`
- `http://127.0.0.1:8000/api/store-profile`
- `http://127.0.0.1:8000/api/store-hours`
- `http://127.0.0.1:8000/api/menu-items`
- `http://127.0.0.1:8000/api/customers`
- `http://127.0.0.1:8000/api/orders`
- `http://127.0.0.1:8000/webhooks/whatsapp/kapso`

When invoking modules directly from source, set {term}`PYTHONPATH` so imports resolve cleanly:

```bash
PYTHONPATH=src uv run -m ruperto --help
```

To simulate a customer conversation without WhatsApp, post to the development
channel endpoint:

```bash
curl -X POST http://127.0.0.1:8000/api/dev/messages \
  -H 'content-type: application/json' \
  -d '{"external_user_id":"cliente-demo","message_text":"Hola, quiero pedir"}'
```

For a brand-new customer, the assistant can answer purely informational menu or
delivery questions without blocking on the person's name. Once a real order is
being built, it still asks for the name if needed and then reuses it across
later messages for the same development identity.
If the first message already contained an order or menu question, that pending
intent is remembered while the assistant asks for the name and then resumed as
soon as the customer identifies themself.
The same applies to denser opening messages like `Hola, soy Martín, mandame 2
pizzas muzza, ¿cuánto es? te pago acá`: the backend now passes safe turn hints
so the assistant can avoid re-asking the name and can reuse explicit payment or
price cues in the same turn.
If the customer asks for a later ready time such as `Quiero una hamburguesa
para las 12`, the order can now stay scheduled for that slot instead of being
treated as immediate. Confirmed summaries also include the configured transfer
alias whenever the chosen payment method is `transferencia`.
Once the checkout details are complete, the assistant now shows a deterministic
review based on the persisted draft and waits for an explicit final
confirmation before closing the order. Informational delivery questions such as
shipping cost or area coverage also stay out of the checkout script instead of
jumping straight to address or payment prompts.

The seeded demo menu now includes a broader synthetic catalog with pizzas,
hamburgers, lomitos, milanesas, wraps, empanadas, drinks, and desserts. That
makes it easier to test more realistic conversations, including follow-up
suggestions such as offering a beverage or a dessert after the customer
chooses a main dish.
Informational menu questions are also grounded more explicitly now, so prompts
like `¿Tenés gaseosas?` should return concrete options and prices instead of a
generic yes/no answer.
Those informational answers also respect simple constraints such as `sin
alcohol`, and correction messages like `uno de cada` are now treated more
carefully when the previous assistant turn mixed variants from multiple
product groups. If the model itself fails on a dense first-turn order, the
backend can now recover some multi-item drafts deterministically instead of
always falling back to a generic handoff.

Or launch the built-in PydanticAI web client for development:

```bash
uv run ruperto web-chat
```

Then open `http://127.0.0.1:7932/`.

In this mode there is no WhatsApp phone number, so Ruperto identifies the user
with a stable development identity derived from the web chat id:
`web:<chat-id>`. If you continue in the same web chat, the stored customer,
order, and conversation history are reused automatically.

For WhatsApp testing through Kapso, the environment variables are now only the
fastest bootstrap path. In the dashboard, open `Configuración del agente` and
fill the Kapso WhatsApp section for the active store. That per-store
configuration is the recommended setup when you want different locals to own
different numbers.

If you want a simpler browser harness that ships inside the main FastAPI app,
open:

- `http://127.0.0.1:8000/demo/chat/<tenant-slug>`

This demo page sends requests to `/api/dev/messages/<tenant-slug>` and lets you
switch between multiple phone numbers, which makes it handy for simulating
known customers with existing conversation memory inside that tenant.

Staff can also move an order through operational statuses from the API, for
example to mark a pickup order as almost ready:

```bash
curl -X PATCH http://127.0.0.1:8000/api/orders/1/status \
  -H 'content-type: application/json' \
  -d '{"status":"almost_ready"}'
```

When that order belongs to a WhatsApp conversation handled through Kapso, the
backend now tries to deliver the matching proactive customer notification
automatically through the same channel instead of waiting for the next inbound
message.
If the customer writes back asking "¿cómo va mi pedido?", the same chat can now
answer from the stored order state without waiting for another operational
update first.

You can also replace the weekly opening-hours schedule. Each weekday accepts
zero or more slots, so leaving a day without open ranges means the store stays
closed that day:

```bash
curl -X PUT http://127.0.0.1:8000/api/store-hours \
  -H 'content-type: application/json' \
  -d '{"hours":[{"weekday":0,"slot_index":0,"opens_at":null,"closes_at":null,"closed":true},{"weekday":1,"slot_index":0,"opens_at":"11:00","closes_at":"15:00","closed":false},{"weekday":1,"slot_index":1,"opens_at":"19:00","closes_at":"23:00","closed":false},{"weekday":6,"slot_index":0,"opens_at":"12:00","closes_at":"15:00","closed":false}]}'
```

When the store is currently closed, customer replies mention the next opening
time automatically.
The same backend also exposes a simple Tailwind dashboard for staff at
`/dashboard`. It now requires a basic email-and-password login backed by a
signed session cookie. The current version is intentionally small: it shows
an operational home page with recent orders and metrics, a dedicated customers
screen with search, and separate settings pages for the menu, store profile,
agent behavior, flexible weekly opening hours, and user roles.
The store profile page shows the tenant vertical and public slug as read-only
identity fields, so the same installation can host different tenant types
without converting one tenant into another from the dashboard.
If you bootstrap a municipal tenant, `init-db` now also seeds a first service
catalog with municipal areas and categories, and the shared chat endpoint
guides neighbors through a first complaint/request intake before creating the
municipal case. That intake only accepts a usable location before submission,
asks for a respectful rephrase if the message becomes insulting, and keeps the
final confirmation brief by addressing the citizen by first name. Once a case
already exists, the same chat can also answer proactive follow-up questions
about the latest case or an explicit `caso #<n>` reference.

If one dashboard user belongs to more than one store, the header lets staff
switch the active store. That switch already scopes the editable store profile
and weekly opening hours, which is the first visible step toward logical
multi-tenancy in the dashboard.
When the assistant decides a conversation needs a person, the backend now marks
that conversation as waiting for a human. New customer messages stop receiving
automatic bot replies, the dashboard customers page surfaces the handoff queue,
and staff can answer from there using the same official WhatsApp connection
owned by the active store. Releasing the handoff returns control to the bot.

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
