# Delegation: which agent for which task

Goal: code with cheaper models, keep the strong models for thinking. Profiles exist for both Claude Code (`.claude/agents/*.md`) and Codex (`.codex/agents/*.toml`), same names, kept in sync (edit both).

| Profile | Claude model / effort | Codex model / effort | Use for |
|---------|-----------------------|----------------------|---------|
| smartpi-explorer | haiku / low | gpt-6-luna / low | locate code, callers, tests; read-only |
| smartpi-developer | sonnet / medium | gpt-6.1-terra / medium | implement a well-specified change + tests |
| smartpi-test-engineer | sonnet / medium | gpt-6.1-terra / medium | write/repair/run tests |
| smartpi-bench-engineer | sonnet / medium | gpt-6.1-terra / medium | build/run frozen benches |
| smartpi-data-analyst | sonnet / medium | gpt-6.1-terra / medium | field exports, replays, evidence tables |
| smartpi-debugger | sonnet / high | gpt-6.1-terra / high | root cause of a user problem (read-only) |
| smartpi-docs-writer | sonnet / medium | gpt-6.1-terra / medium | docs, cards, changelog |
| smartpi-release-manager | sonnet / medium | gpt-6.1-terra / medium | release checklist and notes |
| smartpi-translator | haiku / low | gpt-6-luna / low | FR/EN sync |
| smartpi-senior-developer | opus / high | gpt-6.1-sol / high | regulation/learning/persistence changes needing judgement |
| smartpi-architect | opus / high | gpt-6.1-sol / high | design + implementation spec (read-only) |
| smartpi-reviewer | opus / high | gpt-6.1-sol / high | independent diff review (read-only) |
| smartpi-control-researcher | opus / xhigh | gpt-6.1-sol / xhigh | control theory + thermodynamics research |
| smartpi-fable-consult | fable / high | gpt-6-astra / high | ONLY with the user's explicit approval per use |

Codex model names and the `[agents]` key of `.codex/config.toml` were taken from the vendor's documentation and are UNVERIFIED on a live Codex install (they may change): if a profile fails on an unknown model, adjust the `model` line (or remove it to inherit the parent's).
Codex custom agents inherit the parent's sandbox, and Claude read-only profiles keep a shell tool: "read-only" profiles are read-only by instruction only. Subagents cannot spawn other agents: the orchestrating session chains them (for example docs-writer, then translator). The Fable profiles refuse to start unless the brief states the user's approval.

## Routing rules

1. Default to the cheapest adequate profile. Escalate when a cheaper agent reports it cannot decide, or the change touches regulation behaviour, learning gates or persistence.
2. Typical flow for a feature: architect (spec) -> developer(s) (code) -> test-engineer -> reviewer -> docs-writer -> translator. For a regulation change add: control-researcher (method), bench-engineer (frozen declaration then run).
3. Typical flow for a user problem: debugger -> (data-analyst for exports) -> architect/researcher if it is a design limit -> developer for the fix.
4. **Fable** is an unanchored second opinion for unclear cross-cutting or blocking research questions. Never for coding, benches or routine checks. Ask the user before each use unless they asked for it. Give it evidence files and precise questions, not your conclusions.
5. **Fresh agent per task**, with a complete self-contained brief (goal, files to read, what was ruled out, rules, expected report). Resume an agent only for a short follow-up on the same task; never keep resuming one that accumulated a huge context.
6. **Briefs are tight**: point to files instead of restating context; ask for a short report. Include the standing rules (no commit, no polling, one bench at a time).
7. Do not re-read a large report if the agent's summary is enough. Do not duplicate an agent's work while it runs.
8. Parallel agents only for independent, read-mostly work. Never two benches at once.
9. The orchestrator validates each step before the next, and checks delegated diffs.
