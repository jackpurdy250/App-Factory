# App Factory

A local, deterministic multi-agent pipeline that turns a single command-line
request into reviewed, gated software. You describe a feature; a fixed sequence
of specialized AI roles optimizes the request, specs it, builds it, reviews it,
and decides whether it ships. The whole machine is engineered so it **cannot**
get stuck in infinite debate or perfectionism loops: every rule that guarantees
termination is plain Python, and no model ever votes on whether to stop.

> **Provider-agnostic by construction.** No file in `core/` or `agents/` ever
> names a vendor or a model. Which AI plays which role is a *config-time*
> decision (`config/models.toml`), so you can drop GPT, Claude, Gemini, Grok,
> DeepSeek, Kimi, GLM, or a local model into any seat without touching code.

---

## The three windows

App Factory runs as a small FastAPI server that drives a three-pane browser UI
over a WebSocket. Each pane has exactly one job.

| Window | Name | What it shows | What it never shows |
|---|---|---|---|
| **1** | **Command line** | Your prompt, and terse system replies (`ack`, `ready for review`, `invalid command`, `pipeline busy`, ...). | Model chatter. It speaks a closed vocabulary only. |
| **2** | **Live sandbox** | The compiled result. For a web build, a sandboxed iframe; for every other stack, the source with a file-tree manifest (path, size, sha256, entrypoint marker) and a preview/shipped channel toggle. | AI commentary. |
| **3** | **AI chatter & logs** | Every pipeline step, agent JSON payload, gate decision, and trace, in real time. | Nothing — this is the glass box. |

Window 1 is deliberately dumb: it validates nothing itself and only ever
prints strings from the `CliResponse` vocabulary. All intelligence is in the
Python router and the pipeline behind it.

---

## Quick start

```bash
# 1. Install runtime dependencies (server extras included)
python3 -m pip install -r requirements.txt

# 2. Point each role at a model. Start from the mock config to see the
#    machinery run with no network and no API key:
python3 run.py --check --models config/models.mock.toml

# 3. To use real models, copy the example env and fill in the keys named
#    by your models.toml providers:
cp .env.example .env        # then edit .env
python3 run.py --check       # validates config/models.toml + providers

# 4. Launch the server and open the printed URL (default http://127.0.0.1:8765)
python3 run.py
```

`run.py --check` loads and validates the model config, resolves every provider
preset, and confirms the web assets exist — without opening a socket or
spending a token. Run it after any config edit.

### run.py flags

| Flag | Default | Purpose |
|---|---|---|
| `--models PATH` | `config/models.toml` | Role-to-model bindings (`.toml` or `.json`). |
| `--governor PATH` | `config/governor.toml` | Budgets, gates, rule and context limits. |
| `--env PATH` | `.env` | Where API keys are read from. |
| `--runs PATH` | `./runs` | Where run snapshots and events are written. |
| `--web PATH` | `./web` | Front-end assets to serve. |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address. |
| `--verbose {quiet,normal,trace}` | `normal` | Window 3 detail. `trace` reveals `AGENT_CALL`/`AGENT_OK`. |
| `--check` | — | Validate everything and exit. No server, no tokens. |

---

## Command reference

Commands are parsed by strict Python (regex + string matching), never by a
model. Anything that fails parsing returns `invalid command` to Window 1 with
a reason code in Window 3, and spends zero tokens.

### `#` — execution (these can call models)

| Command | Meaning |
|---|---|
| `#route <TARGET\|ALL> <instruction>` | Start a build (or a fresh run) aimed at one agent or the whole pipeline. |
| `#revise <TARGET> <instruction>` | Send the current build back for another iteration with new guidance. |
| `#ship` | Seal the current head snapshot as the shipped build. Writes no new snapshot. |

Targets accept aliases: `OBSERVER` (`obs`, `brain`), `OPTIMIZER` (`po`,
`prompt-optimizer`), `IMPLEMENTER` (`impl`, `builder`), `QC` (`qa`,
`gatekeeper`), `DESIGN` (`design-critic`, `ux`), `PROMPT_ENGINEER` (`pe`).
`COMMANDER` is not routable.

### `!` — state (never reach a model)

| Command | Meaning |
|---|---|
| `!status` | Pipeline status (`idle`, `running`, `awaiting_review`, `blocked`, `needs_human`, `shipped`). |
| `!log` | Recent Window-3 trace. |
| `!state <TARGET>` | The last envelope a given agent produced. |
| `!budget` | Tokens, iterations, and wall-clock against the ceilings. |
| `!issues` | Open issues and accepted debt. |
| `!rules` | Active corrective rules and their TTLs. |
| `!snapshots` | The snapshot ledger for this run. |
| `!project [new <slug> <stack>]` | List projects, or create a project (type defaults to `app`; set it with `!type`). |
| `!project use <slug>` | Switch to another project, saving the current one first. |
| `!stack [<stack>]` | Read the active stack, or **declare** a new one. |
| `!type [<type>]` | Read the active project type, or **declare** a new one. |
| `!verbose <quiet\|normal\|trace>` | Set Window-3 verbosity. |
| `!reload` | Adopt an edited model config at the next stage boundary. |

`!stack` and `!type` are the only `!` verbs that write. With an argument they
persist the new value to `saved-project-context.json` and update live state; a
mid-run stack change is deferred to the next spec (the running build keeps the
stack it was specced for) and reported as `applies: next run`. Both are refused
while a build is in flight (`pipeline busy`); reads always work.

### Reason codes (Window 3)

`E_SIGIL` (missing/invalid leading `#`/`!`), `E_VERB`, `E_ARITY`, `E_TARGET`,
`E_TARGET_SCOPE`, `E_EMPTY_ARG`, `E_TRAILING`, `E_VALUE`.

---

## Multi-project, polyglot workspace

A workspace holds many projects at once, tracked in `saved-project-context.json`
and owned by the Observer. Each project declares a **stack** and a **project
type** at intake, and every stack carries its own writable-suffix allowlist,
entrypoint preference, and preview mode:

| Stack | Writes | Entrypoint | Preview |
|---|---|---|---|
| `web` | `.html .htm .css .js .mjs .svg` | `index.html` ... | iframe |
| `python` | `.py .pyi .cfg .ini` | `main.py` ... | source |
| `go` | `.go .mod .sum` | `main.go` | source |
| `cpp` | `.cpp .cc .h .hpp .cmake` + `CMakeLists.txt`/`Makefile` | `main.cpp` | source |
| `rust` | `.rs` | `src/main.rs` | source |
| `node` | `.js .mjs .cjs .ts` | `index.js` ... | source |
| `java` | `.java .gradle .properties` | `Main.java` | source |
| `csharp` | `.cs .csproj` | `Program.cs` | source |
| `shell` | `.sh .bash` | `main.sh` | source |
| `other` | (none; entrypoint required) | declared | source |

Project types: `app`, `cli`, `library`, `service`, `script`. Only the `web`
stack renders in an iframe; everything else previews as source, so the file
tree in Window 2 is how you inspect a non-web build.

---

## Provider presets

`config/models.toml` binds each role to a `[provider.*]`, and each provider
names a **preset** — a saved `(wire-format, base_url)` pair — plus an
`api_key`. The wire format is the protocol, not the company: nearly every
vendor speaks the OpenAI `/chat/completions` shape; Anthropic speaks
`/messages`.

Hosted presets (need a key): `openai`, `anthropic`, `google`, `xai`,
`deepseek`, `mistral`, `groq`, `cerebras`, `together`, `fireworks`,
`openrouter`, `perplexity`, `moonshot` (Kimi), `qwen`, `zhipu` (GLM).

Local presets (no key, no network): `ollama`, `lmstudio`, `vllm`, `llamacpp`.

```toml
# config/models.toml — an example seat
[provider.builder]
preset  = "anthropic"                 # (kind, base_url) from the preset catalogue
api_key = "env:ANTHROPIC_API_KEY"     # or declare kind + base_url inline instead

[roles.implementer]
provider = "builder"
model    = "your-model-id-here"
```

See `config/models.recommended.toml` for a full annotated per-role starting
point, and the Model Selection Guide in the docs for the reasoning behind each
seat.

---

## Running the tests

```bash
python3 tests/run_all.py          # whole suite
python3 tests/run_all.py --list   # list suites
python3 tests/test_pipeline.py    # one suite
```

The suite runs against `config/models.mock.toml`, so it needs no network and no
API key. It fails if any test imports `server` (which needs FastAPI). Current
scores:

| Suite | Checks |
|---|---|
| `test_schemas` | 148 |
| `test_state` | 83 |
| `test_config` | 93 |
| `test_parser` | 139 |
| `test_commands` | 126 |
| `test_pipeline` | 69 |
| **total** | **658** |

---

## Architecture at a glance

```
Window 1  ->  CommandRouter (Python)  ->  Pipeline (async stage machine)
                                              |
     project_state.json  <--  StateManager (single writer, region ACLs)
                                              |
   S0 intake -> S1 optimize -> S2 spec -> S3 build -> S4 review
              -> S5 adjudicate -> S6 gate -> {loop | ship | escalate}
```

- **Agents write JSON, not prose** (the Implementer is the one exception — it
  emits code inside `<file>` blocks). The runner stamps the shared envelope;
  the model supplies only its payload.
- **The state bus is a state machine.** Agents never pass text to each other;
  they read a role-specific *projection* and write only the region they own.
- **Snapshots, never rollback.** Each iteration seals one write-once snapshot;
  history is additive.
- **Eight governor rules (G1–G8)** guarantee termination: no debate channel,
  triple budget, severity gate, monotonic progress, repeat-offender cap,
  deterministic precedence, capped rule decay, single-writer ACLs.

---

## Honest scope

The test suite proves the **machinery**: the parser, state bus, gates,
projections, snapshots, workspace, and command router are exercised end to end
against a `MockProvider`. What it does **not** prove is real-model efficacy —
whether a given model writes good code, elicits a good charter, or recovers
from a bad review. That requires a real API key and is the operator's call.
The FastAPI server also cannot run in an environment without `fastapi` /
`uvicorn` installed; `run.py --check` validates its config path without
binding a socket.
