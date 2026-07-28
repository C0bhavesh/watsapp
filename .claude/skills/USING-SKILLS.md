# Using Skills in This Project

Enabled plugin (`~/.claude/settings.json`): **superpowers**. (`claude-mem` is DISABLED — memory is file-based, see below.)

## Superpowers — invoke at these triggers
| When | Skill |
|---|---|
| Any new design / "let's build X" | superpowers:brainstorming |
| Approved design → plan | superpowers:writing-plans |
| Execute a plan | superpowers:subagent-driven-development (or executing-plans) |
| Writing code | superpowers:test-driven-development |
| Any bug / "fix" | superpowers:systematic-debugging |
| Before claiming done | superpowers:verification-before-completion |
| Want a review | superpowers:requesting-code-review |

Rule: if there's even a 1% chance a skill applies, invoke it before acting.

## Memory — file-based
- Project memory (shared, in-repo): `docs/memory/` — `error_learnings.md` (read before work), `component_registry.md` + `api_registry.md` (grep before creating), `observations.md` (owner notes).
- Claude's private auto-memory: `C:\Users\devnp\.claude\projects\e--bhvaesh-automation\memory\` — read its `MEMORY.md` index at session start; add a pointer line when storing a new memory. Cross-session context only — project facts belong in `docs/`.
