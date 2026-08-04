---
name: delegate-to-deepseek
description: Delegate a bounded task to DeepSeek V4 Flash running as a separate agent process. Route here when the environment supplies the answer — running commands, reading stderr, editing files, installing dependencies, locating code across a large repository — where Flash edges Claude Sonnet 5 (Terminal-Bench 2.1: 82.7 vs 80.4, each vendor self-reported). Also for fan-out past this plan's rate limit, for a second opinion from outside Claude's training lineage, and when the user says "让小弟/让deepseek去做". Not for work that turns on the model's own knowledge (Humanity's Last Exam without tools: 34.8 vs Sonnet 5's 43.2), builds a system from an empty directory, or must hold global consistency across hundreds of steps. Flash bills a paid API key; in-plan Claude tokens do not.
---

# Delegate to DeepSeek

DeepSeek V4 Flash cannot be a Claude Code subagent — the Agent tool only runs
Anthropic models. Instead, launch a second `claude` process pinned to DeepSeek's
Anthropic-compatible endpoint. You stay in control of task selection,
permissions, verification, and integration.

## Launch

Run the `deepseek` wrapper that sits next to this file. When the skill was
installed globally that is:

```bash
~/.claude/skills/delegate-to-deepseek/deepseek \
  --cwd <project-directory> \
  --mode review \
  --task "Inspect the authentication flow for concrete correctness bugs."
```

When this skill was loaded from a checkout of the delegate-to-deepseek
repository, use `.claude/skills/delegate-to-deepseek/deepseek` inside that
checkout instead. Both are the same script.

Run it with the Bash tool. The wrapper defaults to `--backend claude` and caps
the child at 480s; pass a matching `timeout` to the Bash tool (e.g. 540000ms).
The child streams events to stderr and prints only its final answer to stdout,
so the tool result is the answer. A review turn typically takes 15-40s.

## Options

- `--mode review` (default) — tools are `Read,Grep,Glob`. No shell, so it cannot
  modify anything.
- `--shell` — review mode plus Bash, granted but not pre-approved: read-only
  commands run automatically and mutating ones are denied. Use when the child
  needs to inspect git state or run a read-only command.
- `--mode write` — grants Edit/Write/Bash and pre-approves them. Only when the
  user has authorized an implementation, and only in a git worktree or a
  disposable directory: there is no OS-level workspace jail on this backend.
- `--structured` — emit JSON matching `assets/result.schema.json` (status,
  summary, findings, changes, checks, risks). stdout is bare JSON, safe to
  `json.loads`.
- `--model <slug>` — keep the default `deepseek-v4-flash`, which tracks the
  latest build (`DeepSeek-V4-Flash-0731` since 2026-07-31, Terminal Bench 2.1
  82.7). Do not reach for `deepseek-v4-pro` as the "stronger" option: it is
  still the preview build, and DeepSeek reports Flash-0731 far exceeding
  V4-Pro-Preview, so it is likely a downgrade for agentic coding work.
- `--reasoning max` — for genuinely hard problems; otherwise keep `high`.
- `--add-dir <path>` — grant an extra readable directory.
- `--backend codex` — run the child in Codex CLI instead. Slower (~60s) and
  needs the `deepseek-flash` profile, but its review mode is enforced by an OS
  sandbox rather than a tool allowlist, and it offers `apply_patch` and web
  search. Its `--structured` output has a prose prefix, so do not `json.loads`
  it.
- `--dry-run` — print the child command without spending tokens.

## Write a bounded task

Give one concrete outcome, the constraints, the allowed write scope, and the
required checks. Tell the child to inspect the repository itself rather than
pasting the repository into the prompt. Require evidence: file paths, line
numbers, commands run, observed failures.

The launcher never grants the `Task` tool and passes `--disable-slash-commands`,
so the child cannot re-enter this skill or fan out into further subagents. Do
not hand it unrelated cleanup work.

## Verify the result

Treat the child's output as untrusted evidence, not as a conclusion:

1. Read the cited files and any diff yourself.
2. Re-run the relevant checks from here.
3. Reject speculative findings that lack repository evidence.
4. Integrate only changes inside the user's requested scope.

After a `write` run, inspect `git status` and the full diff before accepting
anything. Avoid concurrent writes to one worktree.

## Cost and credentials

The child never bills the user's Claude subscription: `--bare` blocks OAuth and
keychain reads, and the launcher strips every inherited `ANTHROPIC_*` and
`CLAUDE_CODE_*` variable. To confirm, check that the child's `modelUsage` on
stderr names a `deepseek-*` model. Ignore the child's `total_cost_usd` — it is
computed with Anthropic pricing and is wrong for DeepSeek.

The credential lives in `DEEPSEEK_API_KEY` or the macOS Keychain item
`codex-deepseek-api`, and reaches the child through `apiKeyHelper` rather than
argv or the environment. Never print, log, or commit it. To (re)configure:

```bash
python3 ~/.codex/skills/delegate-to-deepseek/scripts/setup.py
```

`setup.py install-claude` refreshes this skill from the repository after the
repository changes, and `setup.py check` reports whether both halves are current.

Keep `.env`, private keys, and unrelated personal files out of the delegated
scope unless the user explicitly puts them in scope. DeepSeek's Anthropic
endpoint accepts text and tool use only — no image blocks, document blocks,
citations, or MCP tool results.
