@AGENTS.md

## Claude Code specifics

- Project sub-agents are in `.claude/agents/` (`smartpi-*`); routing rules in `.agents/workflows/delegation.md`. Prefer `model: sonnet` agents for implementation, tests, benches and docs, `haiku` for exploration and translation, `opus` for design, review and research. `smartpi-fable-consult` only with the user's explicit approval for that use.
- Project permissions (`.claude/settings.json`) ask before `git commit`/`git push` and before using the Fable consultation agent.
- Your local auto-memory is per machine; durable project knowledge belongs in `.agents/`, not only in local memory.
