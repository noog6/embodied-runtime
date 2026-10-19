# Mira deployment profiles

`config/mira-agentic.toml` remains the normal deployment configuration, unchanged.
It selects direct OpenAI Responses cognition (`gpt-6.1-sol`); its legacy vision
selection uses `OPENAI_VISION_MODEL`, then `OPENAI_MODEL`, then the runtime default.
The alternatives below explicitly select both models, independently of those
environment defaults. These are deployment files; `profiles/mira.toml` remains
the separate robot identity/body profile.

## Model choices and evidence

Official documentation and public model metadata checked **2026-10-19**.
Compatibility here is documented, **not live-tested** with Mira. No paid
requests or hardware checks were performed for these profiles.

| File under `config/` | Cognition and vision provider/model ID | Credential variable | Evidence |
| --- | --- | --- | --- |
| `mira-openai-astra.toml` | Direct OpenAI / `gpt-6-astra` | `OPENAI_API_KEY` | [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra) |
| `mira-openai-luna.toml` | Direct OpenAI / `gpt-6-luna` | `OPENAI_API_KEY` | [Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| `mira-openai-sol.toml` | Direct OpenAI / `gpt-6.1-sol` | `OPENAI_API_KEY` | [Sol model](https://developers.openai.com/api/docs/models/gpt-6.1-sol) |
| `mira-openrouter-claude.toml` | OpenRouter / `~anthropic/claude-opus-latest` | `OPENROUTER_API_KEY` | [Opus latest alias](https://openrouter.ai/~anthropic/claude-opus-latest) |
| `mira-openrouter-gemini.toml` | OpenRouter / `google/gemini-4-argon` | `OPENROUTER_API_KEY` | [Gemini 4 Argon](https://openrouter.ai/google/gemini-4-argon) |
| `mira-openrouter-glm.toml` | OpenRouter / `~z-ai/glm-flash-latest` (operator-approved Flash alternative) | `OPENROUTER_API_KEY` | [GLM Flash latest alias](https://openrouter.ai/~z-ai/glm-flash-latest) |
| `mira-openrouter-grok.toml` | OpenRouter / `~x-ai/grok-latest` | `OPENROUTER_API_KEY` | [Grok latest alias](https://openrouter.ai/~x-ai/grok-latest) |

OpenAI's [current model guide](https://developers.openai.com/api/docs/guides/latest-model)
and model pages identify the standard Astra, Luna and current Sol IDs above.
All three document image input and function calling through Responses. The
profiles use the existing direct Responses cognition and vision adapters.

The [OpenRouter model metadata](https://openrouter.ai/api/v1/models) advertises
image input, `tools`, and `tool_choice` for the selected Claude, Gemini, and Grok
IDs. None advertises `parallel_tool_calls`, so their compatible cognition tables
explicitly use `parallel_tool_calls_mode = "omit"`. The aliases include the
leading `~`; `anthropic/claude-opus-latest` is not the verified ID. At verification
time, Opus latest targeted `anthropic/claude-opus-5.5` and Grok latest targeted
`x-ai/grok-4.7`. Latest aliases can change their resolved model, capabilities,
and price over time; recheck metadata before deployment. Sharing an alias does
not guarantee that separate cognition and vision requests resolve to the same
snapshot during an upstream alias update.

The requested [main GLM latest alias](https://openrouter.ai/~z-ai/glm-latest)
exists and targets `z-ai/glm-5.3`, but advertises only text input. The operator
explicitly approved the separate multimodal **GLM Flash latest** alias for this
profile. `~z-ai/glm-flash-latest` targets `z-ai/glm-5.3-flash` at verification time
and advertises image input, tools, `tool_choice`, and `parallel_tool_calls`.
Its profile therefore uses `parallel_tool_calls_mode = "disable"`, sending
`parallel_tool_calls=false`. The pinned `z-ai/glm-5.3-flash` is another verified
multimodal alternative, but is not selected. No requested choice remains
unresolved after the operator's Flash approval.

## Launch and operating policy

With Python 3.13+, the optional `openai` and `elevenlabs` dependencies installed,
and credentials delivered using [Secrets and API keys](secrets.md):

```sh
source ~/.config/embodied-runtime/openrouter.env
# Also load ELEVENLABS_API_KEY from its protected environment file.
python main.py --config config/mira-openrouter-claude.toml
```

Substitute any available filename above. Direct profiles require
`OPENAI_API_KEY`; OpenRouter profiles require `OPENROUTER_API_KEY` and explicitly
use `https://openrouter.ai/api/v1` through the existing compatible adapters.
Every alternative also requires `ELEVENLABS_API_KEY`. TOML contains only
credential-variable names, never secret values. Preserve the direct OpenAI
endpoint setup; do not use `OPENAI_BASE_URL` to redirect direct cognition.

Each alternative has `vision = "configured"` with **exactly one explicit
backend**, matching cognition's provider and model ID. There is no alternate
model/provider vision fallback. Compatible cognition deliberately declares
`tool_calling = true` and `image_input = true`, enabling operator attachments
through the existing image-input path. Explicit image interpretation continues
through configured vision; these profiles add no image routing behavior.

All configured vision attempts and total vision deadlines are **60 seconds**.
Compatible cognition also uses a **60-second** request timeout to allow bounded
reasoning and tool-result responses. The same limits apply to all supplied
models; there are no model-specific timeout differences. Direct Responses
cognition retains its existing SDK timeout behavior, which is not a TOML field.
These are operator-selected limits, not measured latency guarantees.

Omitting `parallel_tool_calls` does not permit multiple tool execution: Mira
rejects multiple returned calls before executing any, executes one accepted
call through runtime validation, and rejects tool calls at the final prose
stage. Existing episode budgets and authority boundaries are unchanged.
OpenRouter keeps `require_parameters = true` and `allow_fallbacks = false`;
there is no new provider routing, model discovery, or alias resolver. See
[compatible cognition](cognition-compatible.md),
[client tool requirements](https://openrouter.ai/docs/guides/features/tool-calling),
and [strict provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

Every alternative preserves the normal hardware/camera auto selection,
interaction posture, initiative, memory, conversation history, Jobs, power,
earcons, Toronto timezone, wake settings, and disabled SMS defaults. Speech is
still ElevenLabs `eleven_flash_v2_5`, voice `pFZP5JQG7iQjIQuC4Bku`, speed `1.1`,
with the same lazy eSpeak fallback. Selecting cognition/vision does not select TTS.

All normal model profiles share `data/mira-memory.sqlite3`,
`data/conversations.sqlite3`, `data/jobs.sqlite3`, and derived
`data/jobs-workspaces/`. The default conversation media database also remains
`data/conversation-media.sqlite3` relative to the working directory; launch from
the repository root, as the wizard does. Switching models continues the same
Mira. Stop the existing runtime before switching; do not run these profiles concurrently
against the shared stores.

## Experiments and startup wizard

The tracked configuration inventory before this cleanup was five deployment/
experiment TOMLs in `config/`, `profiles/mira.toml` (robot profile), and
`pyproject.toml` (packaging); `deploy/systemd/embodied-runtime.service.in` remains
the service template. The four experimental files now live under
`config/experiments/`; none was discarded:

| File | Effective storage/resource policy, preserved after relocation |
| --- | --- |
| `procedure-experiment.toml` | Isolated `work/procedure-experiment.sqlite3`; derived `work/procedure-experiment-workspaces/` |
| `shared-reads-pilot.toml` | Isolated `work/shared-reads-pilot.sqlite3`; derived `work/shared-reads-pilot-workspaces/` |
| `reflection-experiment.toml` | Existing Mira `data/jobs.sqlite3` and `data/jobs-workspaces/`; stop the normal runtime first |
| `reflective-continuity-job.toml` | Operator-rendered Job definition template; **not** a runtime `--config`; content unchanged |

The runtime experiments remain virtual, without camera/vision, and seed no
Job definitions at startup. Their database paths now begin `../../` because database paths
resolve relative to the TOML file. No other file-relative resources were present;
derived workspace locations follow the unchanged database locations. No user
data or databases were moved or created by this cleanup.

The [Startup Wizard](startup.md) discovers only top-level `config/*.toml`;
interactive setup still prefers `mira-agentic.toml`. Automation must pass an
explicit `--config`. Runtime experiments remain selectable by their explicit
`config/experiments/...` path; the Job template is deliberately not a candidate.
The wizard discovers compatible cognition's `api_key_env` as well as all active
vision credential names, deduplicating shared credentials. Existing headless
launch rules still apply: these normal profiles enable initiative messaging,
so wizard `--mode run` deployment requires an enabled delivery route, such as
the wizard's explicit `--with-sms` option and its additional Twilio credentials.

## Diagnostics, costs, and remaining validation

Existing `[COGNITION]` and `[VISION]` records distinguish requested `model` from
response `returned_model`; provider-call records retain returned identity when
present. Compare both when investigating aliases. A returned identity is
diagnostic evidence, not a signal to switch models or retry.

OpenRouter documents response-reported charges in `usage.cost` and upstream
cost detail ([usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)).
For the official endpoint, the existing runtime uses valid `usage.cost` account
charges before catalog estimates; it does not add or substitute upstream costs.
Direct OpenAI uses local, versioned catalog estimates, requiring a matching
entry and complete supported usage. A new direct model can have unavailable
estimates. Inspect `cost_basis` per call and summary `status`/`total_usd`:
reported and estimated components can produce a mixed total, and unknown calls
make the complete total unavailable while preserving known subtotals. These
amounts are not invoice reconciliation; see [response cost accounting](openrouter-cost-accounting.md).
This change updates no catalog prices and adds no cost lookup.

Offline tests parse and resolve every supplied runtime profile, construct lazy
cognition/vision adapters, check explicit models against conflicting environment
defaults, compare operating settings/persistence/voice, and check experiment
storage and wizard credentials. They do not establish live success. Before
deployment, an operator still needs to validate account/model access, text and
image requests, one-tool continuation and final prose, timeout suitability,
returned model IDs, voice playback/fallback, and physical camera/hardware
behavior. Such calls can cost money; none were made during this work.
