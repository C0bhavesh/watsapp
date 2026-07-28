# No Secrets (CRITICAL)
- Never hardcode/commit API keys, tokens, or `.env`. Secrets come from the config store (encrypted) or env at deploy.
- Covered secrets: Shopify client id/secret, `shpat_` access tokens, Meta WhatsApp token (`EAA…`), Meta app secret, webhook verify token, LLM keys. Only `APP_MASTER_KEY` + `DATABASE_URL` live in env.
- Encrypt with Fernet (`APP_MASTER_KEY` from env) before storing; never log; never return plaintext to any UI (show "•••• configured").
- Compliance grep after writing any file in `app/`: must return EMPTY:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
