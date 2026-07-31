# Working on this repository

This repository is one skill that two agent harnesses consume differently. It
launches DeepSeek V4 Flash as a bounded subagent inside a separate Codex CLI or
Claude Code process.

## Which harness reads what

| Path | Read by | Notes |
| --- | --- | --- |
| `SKILL.md` | Codex CLI | Skill root; Codex loads `~/.codex/skills/<name>/SKILL.md` |
| `agents/openai.yaml` | Codex CLI | Display name and implicit-invocation policy |
| `.claude/skills/delegate-to-deepseek/` | Claude Code | Project-level skill; also the source copied to `~/.claude/skills/` |
| `AGENTS.md` / `CLAUDE.md` | both | This file |
| `scripts/`, `assets/`, `tests/` | both | Shared implementation |

Claude Code only reads its own skills directory, so it cannot load this
repository in place the way Codex does. `python3 scripts/setup.py install-claude`
copies the two Claude Code files into `~/.claude/skills/delegate-to-deepseek/`.
**Edit the copies under `.claude/` in this repository, never the installed ones**,
then rerun that action. `setup.py check` warns when the two have drifted.

The two `SKILL.md` files are deliberately different documents, not duplicates:
the Codex one defaults to `--backend codex`, the Claude Code one to
`--backend claude`, and each documents the boundaries that apply to its own
harness. Keep facts that belong to both — credential handling, verification
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

- Never print, log, echo, or commit the DeepSeek API key. It lives in
  `DEEPSEEK_API_KEY`, the macOS Keychain item `codex-deepseek-api`, or the
  Windows credential with that target name, and reaches children through a key
  helper rather than through argv or the environment.
- The `claude` backend must keep `--bare` and must keep stripping inherited
  `ANTHROPIC_*` and `CLAUDE_CODE_*` variables. Without both, a child falls back
  to the parent's OAuth credential and bills an Anthropic subscription instead
  of DeepSeek. Verified by `tests/test_claude_backend.py`.
- Never grant the child a `Task`/subagent tool, and keep
  `--disable-slash-commands` set, so a child cannot re-enter this skill.
- `assets/result.schema.json` is shared. Codex resolves its draft 2020-12
  `$schema` meta-reference; Claude Code rejects it, so `claude_json_schema()`
  strips that key at call time. Do not fork the file.
- Windows is supported: no POSIX-only APIs in `scripts/`, and process teardown
  goes through `popen_kwargs()` / `terminate_process_tree()`.
