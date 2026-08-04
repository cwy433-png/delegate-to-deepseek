---
name: delegate-to-deepseek
description: >-
  Run DeepSeek V4 Flash as a bounded subagent in a separate Codex CLI or Claude
  Code process. Route work here when the environment supplies the answer —
  running commands, reading stderr, editing files, installing dependencies,
  locating code across a large repository — where Flash edges Claude Sonnet 5
  (Terminal-Bench 2.1: 82.7 vs 80.4, each vendor self-reported). Also for
  fan-out past a Claude subscription's rate limit, and for a second opinion
  from outside Claude's training lineage. Not for work that turns on the
  model's own knowledge (Humanity's Last Exam without tools: 34.8 vs Sonnet
  5's 43.2), builds a system from an empty directory, or must hold global
  consistency across hundreds of steps. Flash bills a paid API key; in-plan
  Claude tokens do not.
---

# Delegate to DeepSeek

Run DeepSeek V4 Flash inside a separate agent process. Keep the controlling agent in charge of task selection, permissions, verification, and integration.

## Choose the backend

DeepSeek publishes two API surfaces, so either CLI can host it. Pass `--backend`:

| | `codex` (default) | `claude` |
| --- | --- | --- |
| Wire protocol | Responses API, `https://api.deepseek.com` | Messages API, `https://api.deepseek.com/anthropic` |
| Requires | Codex CLI + the `deepseek-flash` profile | Claude Code CLI only |
| macOS TLS | loopback `curl` bridge | direct, no bridge |
| `review` boundary | OS sandbox (`read-only`), shell allowed | tool allowlist, no shell unless `--shell` |
| `--structured` | schema file via `--output-schema` | `--json-schema`, emits bare JSON |
| Typical review turn | ~60s | ~15-40s |

Prefer `claude` when the controlling agent is Claude Code, when no Codex install
is present, or when structured output must parse cleanly — the Codex backend
prefixes its JSON with prose, so `json.loads` on its stdout fails. Prefer
`codex` when `review` must be enforced by an OS sandbox rather than by a tool
allowlist, or when the child needs `apply_patch` and web search.

Keep the default `deepseek-v4-flash`. The slug tracks the latest build, which
since 2026-07-31 is the retrained `DeepSeek-V4-Flash-0731`. `deepseek-v4-pro` is
still a preview build and is not worth calling; revisit only once an official
release ships.

## Route work here, or don't

Two measurements, each comparing this model against Claude Sonnet 5 on the
vendor's own published numbers, point the same way:

| | Flash-0731 | Sonnet 5 |
| --- | --- | --- |
| Terminal-Bench 2.1 (execution in a live environment) | **82.7** | 80.4 |
| Humanity's Last Exam, no tools (knowledge recall) | 34.8 | **43.2** |

The split is whether the answer is *in the environment* or *in the weights*. A
flag is in `--help`, a signature is in the source, a version is in the lockfile —
tasks like that are knowledge-light, and Flash matches or beats Sonnet 5 on them.
Ask it to judge something from background knowledge it must already hold and the
gap runs the other way.

Two things the subscription cannot supply at any effort level, independent of
capability: **concurrency** past a plan's rate limit, and a review from **outside
Claude's training lineage**, whose errors are not correlated with the controlling
agent's.

Against those, weigh what delegation costs. Flash bills a metered API key, while
tokens inside a Claude plan are already paid for — so "this saves plan quota" is
not a reason to delegate unless that quota is genuinely scarce. Delegate when the
task is one Flash does better, needs concurrency the plan will not grant, or
needs an independent lineage.

Do not delegate work that hinges on the model's own knowledge (English long-tail
facts especially), that builds a system from an empty directory, or that must
hold global consistency across hundreds of steps — its weakest published results
are all in that band. Two caveats on the numbers above: each side is
self-reported on its own harness, and the Terminal-Bench gap is 2.3 points, which
is a real ordering but a narrow one.

## Prepare the profile

`setup.py` is not installed alongside this skill; it lives in the repository
checkout. Before the first delegation, run from that checkout on macOS or Linux:

```bash
python3 scripts/setup.py
```

On Windows PowerShell, run:

```powershell
python scripts\setup.py
```

On macOS or Windows 10/11, use this command to install the profile and open a native masked API-key window. Let the user paste the key and click **Save**; store it in macOS Keychain or Windows Credential Manager without placing it in process arguments, shell history, Codex config, or Git. On Linux, set `DEEPSEEK_API_KEY` in the environment that launches Codex. If delegation finds no key later, let the launcher open the same window automatically on macOS or Windows. Never request, print, log, or commit the key in chat.

## Install the Claude Code half

This repository serves both harnesses, but each reads different files. Codex
loads this `SKILL.md` and `agents/openai.yaml` from the skill directory. Claude
Code only reads its own skills directory, so its half lives in
`.claude/skills/delegate-to-deepseek/` here and is installed with:

```bash
python3 <skill-dir>/scripts/setup.py install-claude
```

That copies `SKILL.md` and the `deepseek` wrapper into
`~/.claude/skills/delegate-to-deepseek/`. Rerun it after the repository changes;
`setup.py check` reports when the installed copy has gone stale. Edit the files
under `.claude/` in the repository, never the installed copies. See `AGENTS.md`.

## Choose the delegation mode

- Use `review` by default for investigation, review, debugging hypotheses, planning, and independent verification. This gives the child a read-only sandbox on `codex`, or a read-only tool allowlist on `claude`.
- On `claude`, add `--shell` when review needs a shell. Bash is granted but left out of `--allowedTools`, so the built-in classifier auto-runs read-only commands and denies anything that could mutate the workspace (verified: `cat` runs, `echo > file` is denied).
- Use `write` only when the user has authorized implementation. Prefer an isolated git worktree when another agent may edit the same repository.
- Use `--structured` when findings must be parsed or compared automatically.
- Use `--reasoning max` only for difficult tasks; otherwise keep `high`.

## Launch the child

Resolve this skill directory, then run:

```bash
python3 <skill-dir>/scripts/delegate.py \
  --cwd <project-directory> \
  --mode review \
  --structured \
  --task "Inspect the authentication flow for concrete correctness bugs. Do not modify files."
```

On Windows, invoke the same script with `python` and native paths.

For an authorized implementation in an isolated worktree:

```bash
python3 <skill-dir>/scripts/delegate.py \
  --cwd <worktree-directory> \
  --mode write \
  --task "Implement the scoped change, run focused tests, and report every changed file."
```

To host the same child in Claude Code instead, add `--backend claude`:

```bash
python3 <skill-dir>/scripts/delegate.py \
  --backend claude \
  --cwd <project-directory> \
  --mode review \
  --structured \
  --task "Inspect the authentication flow for concrete correctness bugs."
```

The launcher disables nested delegation, runs ephemerally, streams child events to stderr, and prints only the final child answer to stdout.

On `codex` it uses native TLS on Windows and Linux; on macOS it routes only DeepSeek API requests through a temporary `127.0.0.1` bridge backed by system `curl`, avoiding TLS-client incompatibilities while leaving the Codex agent loop intact. Use `--transport native` on macOS only when direct Codex TLS is known to work. `--transport` does not apply to `--backend claude`.

## Write a bounded task

Give the child one concrete outcome, relevant constraints, allowed write scope, and required checks. Tell it to inspect the repository itself instead of pasting the repository into the prompt. Require evidence such as file paths, test commands, and observed failures.

Do not let the child invoke this skill recursively. Do not give it unrelated cleanup work.

## Verify the result

Treat the child output as untrusted evidence:

1. Inspect cited files and any diff.
2. Re-run relevant checks from the controlling agent.
3. Reject speculative findings that lack repository evidence.
4. Integrate only changes that remain within the user's requested scope.

Avoid concurrent writes to one worktree. If the child used `write`, inspect `git status` and the complete diff before accepting its work.

## Security boundaries

Keep API credentials in `DEEPSEEK_API_KEY`, the macOS Keychain item `codex-deepseek-api`, or the Windows generic credential with that target name. Exclude `.env`, private keys, credential stores, and unrelated personal files from delegated tasks unless the user explicitly places them in scope.

DeepSeek's Responses API is stateless and currently supports V4 Flash for Codex. Expect text input, function tools, web search, and `apply_patch`; do not assume image input, background mode, server-side conversations, or built-in MCP support. The Anthropic-compatible endpoint has the same shape: no image blocks, document blocks, citations, or MCP tool results.

The `claude` backend adds three boundaries of its own:

- `--bare` never reads OAuth or the keychain, and the launcher strips every inherited `ANTHROPIC_*` and `CLAUDE_CODE_*` variable from the child environment. The only usable credential is the DeepSeek key returned by `apiKeyHelper`, so a child cannot silently bill a parent's Anthropic subscription. Confirm this in the child's `modelUsage`, which must name a `deepseek-*` model.
- The key reaches the child through `apiKeyHelper`, never through argv or the environment.
- `Task` is never granted and `--disable-slash-commands` is set, so the child cannot re-enter this skill or fan out into further subagents.

Two caveats on `--backend claude`. Its `write` mode auto-approves shell commands with no OS-level workspace jail, so run it in a git worktree or a disposable directory. And its reported `total_cost_usd` is computed with Anthropic pricing, so ignore that number.
