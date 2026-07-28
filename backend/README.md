# Thetavas Order Bot — Backend

Phase 1: skeleton + Shopify layer. See `docs/architecture-plan.md` (v1.1) and
`docs/architecture-decisions.md` (ADRs 001–005).

## Dev setup (from backend/)
1. `python -m pip install -r requirements.txt`
2. Create `.env`: `APP_MASTER_KEY=<Fernet key>` (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
3. `python -m pytest` · `ruff check .` · `mypy app`

## Live smoke (optional, dev-only)
Add `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` to `.env`, then:
`python -m scripts.smoke_shopify`
