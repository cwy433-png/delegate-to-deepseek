---
name: delegate-to-deepseek
description: Launch DeepSeek V4 Flash through a separate Codex CLI Responses API profile as a bounded coding subagent. Use when the user asks Codex to delegate work to DeepSeek, obtain an independent code review or second opinion, explore large code or log context, compare model conclusions, or let DeepSeek investigate, implement, or test a scoped repository task.
---

# Delegate to DeepSeek

Run DeepSeek V4 Flash inside a separate Codex CLI process. Keep the current Codex agent in control of task selection, permissions, verification, and integration.

## Prepare the profile

Before the first delegation, run on macOS or Linux:

```bash
python3 ~/.codex/skills/delegate-to-deepseek/scripts/setup.py
```

On Windows PowerShell, run:

```powershell
python "$HOME\.codex\skills\delegate-to-deepseek\scripts\setup.py"
```

On macOS or Windows 10/11, use this command to install the profile and open a native masked API-key window. Let the user paste the key and click **Save**; store it in macOS Keychain or Windows Credential Manager without placing it in process arguments, shell history, Codex config, or Git. On Linux, set `DEEPSEEK_API_KEY` in the environment that launches Codex. If delegation finds no key later, let the launcher open the same window automatically on macOS or Windows. Never request, print, log, or commit the key in chat.

## Choose the delegation mode

- Use `review` by default for investigation, review, debugging hypotheses, planning, and independent verification. This gives the child a read-only sandbox.
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

The launcher disables nested multi-agent delegation, runs ephemerally, streams child events to stderr, and prints only the final child answer to stdout. It uses native Codex TLS on Windows and Linux. On macOS it routes only DeepSeek API requests through a temporary `127.0.0.1` bridge backed by system `curl`, avoiding TLS-client incompatibilities while leaving the Codex agent loop intact. Use `--transport native` on macOS only when direct Codex TLS is known to work.

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

DeepSeek's Responses API is stateless and currently supports V4 Flash for Codex. Expect text input, function tools, web search, and `apply_patch`; do not assume image input, background mode, server-side conversations, or built-in MCP support.

## Optional GUI preview

For users who should not need a terminal, launch `DeepCodex.command` on macOS or
`DeepCodex.cmd` on Windows. The preview opens a localhost-only browser GUI,
starts Codex App Server with an isolated `~/.deepcodex` home, pins DeepSeek V4
Flash as the primary model, and provides workspace selection, streaming chat,
stop, diff, and approval dialogs. Its protocol adapter lives in
`scripts/app_server.py`; App Server is experimental, so regenerate its schemas
and rerun the tests after upgrading Codex.
