# CODEBUDDY.md

Shared repository instructions live in `AGENTS.md`, which WorkBuddy loads
through the import below. WorkBuddy stops after finding `CODEBUDDY.md` at a
directory level, so removing the import would hide the shared instructions.
This file holds only what is true of WorkBuddy, so Codex and Claude Code
sessions do not load it.

@AGENTS.md

## The DeepSeek direct provider

`.codebuddy/models.json` is the version-controlled source for the custom
`deepseek-v4-flash` model. Its URL is the complete OpenAI-compatible Chat
Completions endpoint, `https://api.deepseek.com/v1/chat/completions`; WorkBuddy
sends the model id verbatim to DeepSeek, so do not replace the official slug
with a local alias.

`.codebuddy/agents/deepseek.md` defines the native `deepseek` subagent. Its
frontmatter pins WorkBuddy's internal `custom-local:deepseek-v4-flash` selector
so it cannot collide with the built-in model of the same name. The custom model
entry itself keeps the bare API id above; WorkBuddy removes the internal prefix
before sending the request. The agent deliberately omits agent/delegation tools,
so the child cannot recursively fan out. The main WorkBuddy agent sees its name
and description, while the full body becomes the child's instructions only when
the agent is invoked.

Verified against the live API: `POST /chat/completions` returns
`finish_reason: "tool_calls"` with well-formed `tool_calls`, so tool use works
on this path. Both `https://api.deepseek.com` and `.../v1` resolve.

## Setup

```bash
python3 scripts/setup.py install-workbuddy
```

This performs three user-level installations while leaving unrelated settings,
models, and agents untouched:

- Merge the model into `~/.codebuddy/models.json`.
- Copy the managed agent to `~/.codebuddy/agents/deepseek.md`.
- Merge `DEEPSEEK_API_KEY` into the `env` block of
  `~/.workbuddy/settings.json`. The value comes from the macOS Keychain item
  `codex-deepseek-api` or the Windows credential with that name; if no key is
  stored yet, the same masked dialog opens first.
- Set `CODEBUDDY_SMALL_FAST_MODEL=custom-local:deepseek-v4-flash`. WorkBuddy's
  Agent tool cannot choose a model per invocation, and built-in subagents such
  as Explore request the `lite` variant, so this global mapping is what makes
  ordinary automatic delegation use the direct DeepSeek model. The
  `custom-local:` prefix is required to avoid WorkBuddy's built-in model with
  the same bare id.
- On macOS, preserve existing `NODE_OPTIONS` and add
  `--dns-result-order=ipv6first`. DeepSeek's IPv4 edge can reject the TLS
  handshake while its IPv6 edge remains reachable; WorkBuddy's bundled Node
  otherwise selects the failing address first.

Restart WorkBuddy afterwards so it reloads the global model, agent, and
environment. Its normal `lite` subagents will then use DeepSeek automatically;
you can still ask it to “use the deepseek agent” when you want an explicit,
named delegation.

## Known limits

- **The key is stored in plaintext** in `~/.workbuddy/settings.json`. Custom
  model entries resolve `apiKey` only through `${ENV_VAR}` expansion from
  `process.env`; there is no helper-command hook the way Codex and Claude Code
  have one (`apiKeyHelper` covers WorkBuddy's own product token, not custom
  models). This is a real downgrade from the Keychain-only handling everywhere
  else in this repository. Both project and global `models.json` stay clean
  because they contain only `${DEEPSEEK_API_KEY}`.
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

1. `~/.workbuddy/logs/` — `[CustomModelsProductProvider]` should report
   `~/.codebuddy/models.json`; the resolver should report
   `Using env variable model for lite: custom-local:deepseek-v4-flash`.
   `[AgentModelResolver]` should also resolve the named `deepseek` agent to that
   same internal id, not the built-in model with the same bare id and not the
   main model.
2. `~/.workbuddy/traces/*/trace_*.json` — the `generation` spans carry the real
   request and response. `toolOutput` is the raw provider reply; its `model`
   field is authoritative. Note `toolInput` is truncated at 100 KB and omits the
   system prompt, so never treat it as the full request body.
