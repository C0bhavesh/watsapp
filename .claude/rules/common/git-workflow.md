# Git Workflow
- Repo not initialized yet — `git init` (with owner's OK) before the first code commit.
- Small, focused commits per task; conventional messages (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).
- Never commit secrets/.env. Run `ruff` + `pytest` green before committing app code.
- Branch off the default branch for features; do not force-push shared branches.
- **NEVER `git push` without explicit owner approval** — Vercel auto-deploys from `main`.
- Decide with the owner whether `docs/` and `.claude/` are committed or gitignored (cafe project kept them private) before the first commit.
