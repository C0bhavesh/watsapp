# Python Style (ENFORCED)
- Python 3.12+, full type hints on every function signature. `mypy` clean.
- `ruff` is the linter+formatter. No `print()` in app code — use `logging`.
- No bare `except:` — catch specific exceptions; wrap external calls with explicit timeout + typed error.
- Pydantic v2 models for all request/response/config validation. `async def` for I/O.
- FastAPI dependency injection via `Depends` — no global mutable state for per-request data.
- Naming: `snake_case` functions/vars, `PascalCase` classes, `UPPER_SNAKE` constants.
