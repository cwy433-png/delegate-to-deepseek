# CLAUDE.md

Shared repository instructions live in `AGENTS.md`. This file holds only what is
true of Claude Code, so Codex and WorkBuddy sessions do not load it.

@AGENTS.md

## Installing the Claude Code half

Claude Code only reads its own skills directory, so it cannot load this
repository in place the way Codex does:

```bash
python3 scripts/setup.py install-claude
```

That copies `SKILL.md` and the `deepseek` wrapper from
`.claude/skills/delegate-to-deepseek/` into `~/.claude/skills/delegate-to-deepseek/`.
**Edit the copies under `.claude/` in this repository, never the installed
ones**, then rerun that action. `setup.py check` warns when the two have
drifted.

## The credential invariant

The `claude` backend must keep `--bare` and must keep stripping inherited
`ANTHROPIC_*` and `CLAUDE_CODE_*` variables. Without both, a child falls back to
the parent's OAuth credential and bills an Anthropic subscription instead of
DeepSeek — observed directly, not theorised. Verified by
`tests/test_claude_backend.py`; do not relax either half.

Confirm any change here by checking that a child's `modelUsage` names a
`deepseek-*` model. Ignore the child's `total_cost_usd`: it is computed with
Anthropic pricing and is meaningless for DeepSeek.

## Tool boundaries

`--tools` decides which tools exist; `--allowedTools` decides which run without
approval. They are separate axes, and conflating them breaks both modes:

- `write` must grant both, or the child stalls retrying commands the permission
  gate keeps denying.
- `review --shell` deliberately grants Bash **without** pre-approving it, so the
  built-in classifier auto-runs read-only commands and denies anything that
  could mutate the workspace.
