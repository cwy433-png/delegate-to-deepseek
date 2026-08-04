# Working on this repository

This repository is one skill that several agent harnesses consume differently.
It launches DeepSeek V4 Flash as a bounded subagent inside a separate Codex CLI
or Claude Code process.

**This file holds only what every harness needs.** Anything true of one harness
alone belongs in that harness's own file, so the others do not pay for it in
every session:

| Scope | File | Loaded by |
| --- | --- | --- |
| Shared | `AGENTS.md` | Codex, Claude Code (via `CLAUDE.md`), WorkBuddy |
| Claude Code only | `CLAUDE.md` | Claude Code |
| WorkBuddy only | `CODEBUDDY.md`, `.codebuddy/` | WorkBuddy |

Codex has no private project-instruction file — it reads `AGENTS.md` and nothing
else — so the rare Codex-only note stays here, explicitly labelled.

## Layout

| Path | Read by | Notes |
| --- | --- | --- |
| `SKILL.md` | Codex CLI | Source copied to `~/.codex/skills/<name>/` by `install-codex` |
| `agents/openai.yaml` | Codex CLI | Display name and implicit-invocation policy |
| `.claude/skills/delegate-to-deepseek/` | Claude Code | Project-level skill, and the source copied to `~/.claude/skills/` |
| `.codebuddy/models.json` | WorkBuddy | Canonical custom-model entry copied into the user catalog |
| `.codebuddy/agents/deepseek.md` | WorkBuddy | Canonical native subagent copied into the user agents directory |
| `scripts/`, `assets/`, `tests/` | all | Shared implementation |

The Codex and Claude Code `SKILL.md` files are deliberately different documents,
not duplicates: each defaults to its own backend and documents the boundaries
that apply to its own harness. WorkBuddy uses a native custom agent instead.
Keep facts that belong to all of them — credential handling, verification
discipline — consistent between them.

## Backends

`scripts/delegate.py` builds a child command for either harness. DeepSeek serves
both wire protocols, so neither path is a shim:

- `--backend codex` — Responses API at `https://api.deepseek.com`, via the
  `deepseek-flash` Codex profile. On macOS it tunnels through the loopback
  `curl` bridge in `scripts/curl_bridge.py`.
- `--backend claude` — Messages API at `https://api.deepseek.com/anthropic`,
  reached directly.

Anything shared between them belongs in `main()`; anything harness-specific
belongs in `build_codex_plan` or `build_claude_plan`.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

Use `--dry-run` to inspect a child command without spending tokens.

## Conventions

- Never print, log, echo, or commit the DeepSeek API key. Each harness documents
  its own credential storage and transport; do not silently weaken those
  harness-specific boundaries.
- Never grant a child a `Task`/subagent tool, and keep nested delegation
  disabled, so a child cannot re-enter this skill.
- `assets/result.schema.json` is shared. Codex resolves its draft 2020-12
  `$schema` meta-reference; Claude Code rejects it, so `claude_json_schema()`
  strips that key at call time. Do not fork the file.
- Windows is supported: no POSIX-only APIs in `scripts/`, and process teardown
  goes through `popen_kwargs()` / `terminate_process_tree()`.
- Installers must be non-destructive. They write atomically and refuse to
  overwrite a file this repository did not write; config files belonging to
  another product are merged, never replaced.
