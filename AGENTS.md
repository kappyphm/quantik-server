# Repository Guidelines

## Project Structure & Module Organization

This repository contains design documents, preserved Python prototypes, and an in-progress backend.

- `docs/saas-plan/`: requirements, decisions, architecture, pipelines, API contracts, and delivery gates. Start with `README.md` and `01-decisions.md`.
- `docs/prototype/`: reference snapshots (`crawl_data.py`, `quant.py`, `quant_visuals.py`, `backtest.py`). Preserve these files for comparison; implement extracted functionality separately.
- `backend/src/service/`: API, identity, database, jobs/quota and report contracts. `backend/migrations/` contains Alembic migrations; `backend/tests/` contains focused critical checks.
- Planned: `deploy/` and `load-tests/`. Create directories when implementation needs them.

Keep quant engines independent of providers, persistence, and accounts. Put input resolution in analysis orchestration and provider access in market-data adapters.

## Build, Test, and Development Commands

Run commands from the repository root:

```powershell
rg --files                         # List repository files
rg -n 'OPEN|PROPOSED' docs/saas-plan # Find unresolved decisions
python -c "import ast,pathlib; [ast.parse(p.read_text(encoding='utf-8-sig'), filename=str(p)) for p in pathlib.Path('docs/prototype').glob('*.py')]"
```

The Python command checks syntax without importing prototypes or fetching data. Backend install/run/test commands are in root README.md and backend/pyproject.toml. Activate the shared $HOME/.venv before Python commands. Docker deployment resources and the verified trial release report are in deploy/production/; follow its README and keep secrets outside build contexts.

## Coding Style & Naming Conventions

For new Python code, use four-space indentation, `snake_case` modules/functions, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Add type hints at module boundaries. No formatter or linter is configured yet.

Use explicit units: prices in VND, `_pct` versus `_ratio`, UTC timestamps, and separate trading-session dates. Schedule using `Asia/Ho_Chi_Minh`. Follow the numbered Markdown naming pattern in `docs/saas-plan/` and keep relative links valid.

## Testing Guidelines

Minimize testing effort and AI token usage. Tests are optional for documentation, formatting, and small reversible changes; a brief inspection is sufficient. Do not add a test framework, coverage target, or new tests by default.

For behavior changes, use the smallest relevant existing test or a focused syntax/smoke check when useful. Avoid full-suite runs, repeated checks after success, and verbose output. For authentication, billing, data integrity, or numerical logic, verify the affected critical behavior with one focused check; expand only if a failure or unresolved concern justifies it.

If tests are added, prefer pytest and `test_<behavior>.py` under `backend/tests/`. State briefly what was checked or that tests were skipped; never claim unperformed validation. The delivery acceptance matrix applies at the relevant release gate, not to every edit.

## Commit & Pull Request Guidelines

Git is initialized on main. Commit each completed independent component or flow as requested by the user. Use concise imperative messages, preferably `docs:`, `feat:`, or `fix:` prefixes.

PRs should explain the change, link relevant requirement/decision IDs or issues, record verification and limitations, and identify scientific or policy changes. Include screenshots only for visual changes.

## Security & Agent Instructions

Keep credentials out of source, reports, and logs. Never mark `PROPOSED` or `OPEN` decisions as confirmed without evidence. Documentation work does not require package installation or provider onboarding. Add validation before enabling billing policies or publishing models.
