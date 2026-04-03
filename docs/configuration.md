# Configuration (Reference)

This chapter is a lookup reference for environment variables used by commands,
documentation examples, and CI workflows.

```{glossary}
RUPERTO_ENVIRONMENT
  Runtime environment name for the service.
  Use values such as `development`, `test`, or `production`.

RUPERTO_DATABASE_URL
  SQLAlchemy connection string used by the backend.
  The current prototype defaults to SQLite via `aiosqlite`.

RUPERTO_AUTO_INIT_DB
  When enabled, the FastAPI lifespan creates the schema and bootstraps the
  initial store profile on startup.

RUPERTO_STORE_NAME
  Public name of the food business served by the assistant.

RUPERTO_BOT_NAME
  Display name used by the assistant persona.

RUPERTO_STORE_LOCATION
  Optional location or area served by the business.

RUPERTO_STORE_DESCRIPTION
  Short store description that can be reused in the assistant instructions and
  future admin screens.

RUPERTO_ASSISTANT_PERSONALITY
  Short description of the assistant tone and behavior.

RUPERTO_STORE_LOCALE
  Primary locale used by the business-facing configuration and the assistant.
  The current default is `es-AR`, which is the main language for customer and
  staff interactions in the MVP.

RUPERTO_DEFAULT_STORE_ID
  Store identifier resolved by default when the service boots without an
  explicit tenant resolution step.
  This keeps today's single-store MVP configurable while preparing for future
  logical multi-tenancy.

RUPERTO_GEMINI_MODEL
  Google Gemini model name used by the PydanticAI ordering assistant.
  The initial value is `gemini-2.5-flash`.

RUPERTO_GEMINI_API_KEY
  API key for the Google Gemini provider.
  This is intentionally not echoed back by CLI diagnostics.

RUPERTO_ASSISTANT_MODEL_TIMEOUT_SECONDS
  Maximum number of seconds the backend waits for the configured model before
  degrading the turn into a friendly handoff response.

RUPERTO_KAPSO_API_KEY
  API key for Kapso WhatsApp operations.
  This is reserved for the future channel adapter work.

RUPERTO_KAPSO_PHONE_NUMBER_ID
  Kapso or WhatsApp phone number identifier used by outbound messaging.

PYTHONPATH
  Python import search path.
  In this project docs, it is used for module execution from source (for example `PYTHONPATH=src uv run -m ...`).

GH_TOKEN
  Token consumed by the GitHub CLI (`gh`) for authenticated API operations.
  Useful for non-interactive runs such as manual workflow dispatch from CI or scripts.

GITHUB_TOKEN
  Ephemeral token automatically injected by GitHub Actions jobs.
  Used by workflows to interact with repository APIs with job-scoped permissions.

NO_COLOR
  De-facto standard variable used by CLI tools to disable ANSI colors.
  Prefer this for plain-text logs where color escape sequences are undesirable.

FORCE_COLOR
  Variable used by many CLIs to force color output even in non-interactive contexts.
  Use only when colorized logs improve readability and your environment strips correctly.
```
