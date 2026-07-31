# CODEBUDDY.md

Shared repository instructions live in `AGENTS.md`, which WorkBuddy loads
automatically. This file holds only what is true of WorkBuddy, so Codex and
Claude Code sessions do not load it.

## The DeepSeek direct provider

`.codebuddy/models.json` defines `deepseek-v4-flash-direct`, a custom model that
talks to `https://api.deepseek.com` instead of the `copilot.tencent.com`
gateway. It exists for one reason: the gateway's built-in `deepseek-v4-flash`
gives no way to know which build you are on, and DeepSeek shipped a retrained
`DeepSeek-V4-Flash-0731` on 2026-07-31. Going direct, the slug tracks the latest
build, so you know what you are running — at the cost of paying DeepSeek instead
of spending subscription credits.

Verified against the live API: `POST /chat/completions` returns
`finish_reason: "tool_calls"` with well-formed `tool_calls`, so tool use works
on this path. Both `https://api.deepseek.com` and `.../v1` resolve.

## Setup

```bash
python3 scripts/setup.py install-workbuddy
```

This merges two variables into the `env` block of `~/.workbuddy/settings.json`,
leaving every other setting untouched:

- `DEEPSEEK_API_KEY` — read from the macOS Keychain item `codex-deepseek-api` or
  the Windows credential with that name. If no key is stored yet, the same
  masked dialog the rest of this repository uses opens first.
- `CODEBUDDY_SMALL_FAST_MODEL=deepseek-v4-flash-direct` — maps the `lite`
  variant, so `Explore` sub-agents run on DeepSeek while the main session keeps
  whatever model you selected.

Restart WorkBuddy afterwards: `${...}` values are resolved from `process.env`,
and a GUI app launched from Finder does not inherit a shell's environment.

## Known limits

- **The key is stored in plaintext** in `~/.workbuddy/settings.json`. Custom
  model entries resolve `apiKey` only through `${ENV_VAR}` expansion from
  `process.env`; there is no helper-command hook the way Codex and Claude Code
  have one (`apiKeyHelper` covers WorkBuddy's own product token, not custom
  models). This is a real downgrade from the Keychain-only handling everywhere
  else in this repository. `.codebuddy/models.json` itself stays clean and
  committable because it only ever contains `${DEEPSEEK_API_KEY}`.
- **Reasoning continuity across turns is unverified on this path.** WorkBuddy
  echoes prior reasoning back to the gateway in a `reasoning` field; DeepSeek
  emits and expects `reasoning_content`. Whether the direct path preserves the
  chain has not been measured. Check it the same way the gateway was checked:
  compare `usage.prompt_tokens` growth between consecutive `generation` spans in
  `~/.workbuddy/traces/` against the previous turn's `completion_tokens`.
- A variant maps to exactly one model id. `relatedModels` values are strings,
  not arrays, so `lite` cannot fan out across several models.

## Verifying a change

Do not trust the model picker: a slug appearing in the list proves nothing about
which build answers, or whether the custom entry loaded at all.

1. `~/.workbuddy/logs/` — `[CustomModelsProductProvider]` reports the config
   path it read; `[AgentModelResolver]` reports what a sub-agent resolved to.
   `resolved_models` should name the custom id rather than `lite`.
2. `~/.workbuddy/traces/*/trace_*.json` — the `generation` spans carry the real
   request and response. `toolOutput` is the raw provider reply; its `model`
   field is authoritative. Note `toolInput` is truncated at 100 KB and omits the
   system prompt, so never treat it as the full request body.
