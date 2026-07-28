# Agent Orchestra — Routing

| Trigger (human message) | Agent | Notes |
|---|---|---|
| "develop / implement / build / add [X]", "fix [X]" (scope clear) | `developer` | ONLY agent that writes app code |
| `bug::` / `issue::` / "fix" (data/behaviour bug) | `systematic-debugger` | First. Raw bug only; evidence gate before fix |
| "plan / design / architecture for [X]" | `architect` | Read-only design + ADRs; no code |
| After `developer` marks REVIEW | `code-reviewer` | Scoped to changed files |
| Sensitive surface (creds, webhooks, mutations, auth, CORS, store) | `security-reviewer` | Conditional, after code-reviewer |
| Writing/adjusting tests | `tdd-guide` | RED→GREEN discipline |
| After a feature reaches REVIEW | `doc-updater` | Updates registries + status docs |

Rules:
- Each agent runs its error_learnings + registry pre-check first.
- Main Claude routes, coordinates, presents — it does NOT write app code.
- Code-writing is always delegated to `developer`, even one-line changes.
- An agent file without an entry here is invisible — keep this table in sync with `.claude/agents/`.
