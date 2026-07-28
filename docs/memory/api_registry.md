# API Registry

> Every HTTP endpoint and external integration. Grep before adding a new route or external call — never create a parallel implementation.

## Format
## [METHOD /path]
- **Handler:** app/path/to/router.py
- **Request:** pydantic model
- **Response:** pydantic model
- **Notes:** auth, HMAC, rate limit, idempotency

---
<!-- entries below -->
