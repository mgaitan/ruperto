# ruperto

[![CI](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml/badge.svg)](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml)
[![docs](https://img.shields.io/badge/docs-blue.svg?style=flat)](https://mgaitan.github.io/ruperto/)
[![pypi version](https://img.shields.io/pypi/v/ruperto.svg)](https://pypi.org/project/ruperto/)
[![Changelog](https://img.shields.io/github/v/release/mgaitan/ruperto?include_prereleases&label=changelog)](https://github.com/mgaitan/ruperto/releases)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/mgaitan/ruperto/actions/workflows/ci.yml)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/mgaitan/ruperto/blob/main/LICENSE)

A minimalist configurable agent

## Quick Start

Run directly without installing via `uvx`:

```bash
uvx ruperto
```

To install the tool permanently:

```bash
uv tool install ruperto
```

## Development

- Install dependencies with `uv sync`.
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
