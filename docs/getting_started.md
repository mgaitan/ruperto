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

The bootstrap profile defaults to an `es-AR` locale so the MVP can keep
customer and staff-facing interactions in Spanish (Argentina) from the start.
It also respects {term}`RUPERTO_DEFAULT_STORE_ID`, which is the first step
toward a future logical multi-tenant deployment while keeping a single store
active by default today.

## 3. Run the API locally

```bash
uv run fastapi dev
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/healthz`
- `http://127.0.0.1:8000/api/store-profile`
- `http://127.0.0.1:8000/api/store-hours`
- `http://127.0.0.1:8000/api/menu-items`
- `http://127.0.0.1:8000/api/customers`
- `http://127.0.0.1:8000/api/orders`

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

For a brand-new customer, the assistant first asks for the person's name before
continuing with the order flow, unless the opening message already contains a
self-introduction such as `Hola, soy Martín`. That name is then reused across
later messages for the same development identity.
If the first message already contained an order or menu question, that pending
intent is remembered while the assistant asks for the name and then resumed as
soon as the customer identifies themself.
The same applies to denser opening messages like `Hola, soy Martín, mandame 2
pizzas muzza, ¿cuánto es? te pago acá`: the backend now passes safe turn hints
so the assistant can avoid re-asking the name and can reuse explicit payment or
price cues in the same turn.

The seeded demo menu now includes a broader synthetic catalog with pizzas,
hamburgers, lomitos, milanesas, wraps, empanadas, drinks, and desserts. That
makes it easier to test more realistic conversations, including follow-up
suggestions such as offering a beverage or a dessert after the customer
chooses a main dish.

Or launch the built-in PydanticAI web client for development:

```bash
uv run ruperto web-chat
```

Then open `http://127.0.0.1:7932/`.

In this mode there is no WhatsApp phone number, so Ruperto identifies the user
with a stable development identity derived from the web chat id:
`web:<chat-id>`. If you continue in the same web chat, the stored customer,
order, and conversation history are reused automatically.

Staff can also move an order through operational statuses from the API, for
example to mark a pickup order as almost ready:

```bash
curl -X PATCH http://127.0.0.1:8000/api/orders/1/status \
  -H 'content-type: application/json' \
  -d '{"status":"almost_ready"}'
```

You can also replace the weekly opening-hours schedule:

```bash
curl -X PUT http://127.0.0.1:8000/api/store-hours \
  -H 'content-type: application/json' \
  -d '{"hours":[{"weekday":0,"opens_at":"11:00","closes_at":"23:00","closed":false},{"weekday":6,"opens_at":"19:00","closes_at":"23:00","closed":false}]}'
```

When the store is currently closed, customer replies mention the next opening
time automatically.

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
