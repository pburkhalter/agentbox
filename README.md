# agentbox

A container that keeps AI coding agents running on a home server: Claude Code
and OpenAI Codex, both reachable from the phone through their own remote
control, plus [CloudCLI](https://github.com/siteboon/claudecodeui) as a web UI
for both. Optionally, [cli-bridge](#cli-bridge-optional-off-by-default)
offers the two CLIs as an OpenAI-compatible API to one program of yours, such
as an LLM gateway. It runs as a normal user, holds no credentials until you log
in, and updates by being rebuilt, never by the tools updating themselves.

```
phone   Claude app  ⇄ Anthropic ⇠ claude-rc  claude remote-control  (tmux "claude")
phone   ChatGPT app ⇄ OpenAI    ⇠ codex-rc   codex app-server --remote-control
browser (LAN / VPN) ───── :3001 → cloudcli   CloudCLI: chat, shell, files, git
gateway (LAN, token) ──── :3002 → cli-bridge OpenAI API → claude -p / codex exec (off by default)

s6-svscan (PID 1) supervises the services. The remote controls only connect
out. State: /home/agent (logins, tool config), /workspace (repos).
```

| Service     | What runs                                                        | Before you log in        |
|-------------|------------------------------------------------------------------|--------------------------|
| `cloudcli`  | `cloudcli start --port 3001`                                     | runs                     |
| `claude-rc` | `claude remote-control --spawn same-dir --permission-mode default` in `/workspace`, inside tmux session `claude` | idles, checks every 30 s |
| `codex-rc`  | `codex app-server --remote-control --listen unix://` (the server `codex remote-control start` would launch) | idles, checks every 30 s |
| `cli-bridge` | OpenAI-compatible API on 3002, one CLI process per request ([below](#cli-bridge-optional-off-by-default)) | off unless `AGENTBOX_CLI_BRIDGE=1` |

Included tools: `claude`, `codex`, `cloudcli`, git, gh, openssh-client, tmux,
ripgrep, jq, curl, Node.js 26 LTS, Python 3, and `agentbox` (see
[Daily use](#daily-use)). No browsers, no compilers.

## Run

```yaml
# compose.example.yaml has the full file
services:
  agentbox:
    image: ghcr.io/pburkhalter/agentbox:latest
    hostname: agentbox
    user: "568:568"
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    stop_grace_period: 30s
    ports: ["192.168.1.10:3001:3001"] # LAN address only
    volumes:
      - /mnt/tank/apps/agentbox/home:/home/agent
      - /mnt/tank/apps/agentbox/workspace:/workspace
```

Both host directories must belong to the container user (`chown 568:568`).
An empty directory is fine: the first start creates what is missing and never
overwrites what is there.

| Mount          | Holds                                                                 |
|----------------|-----------------------------------------------------------------------|
| `/home/agent`  | Claude (`.claude/`, `CLAUDE_CONFIG_DIR`, incl. `.claude.json`), Codex (`.codex/`), CloudCLI's login database (`.cloudcli/auth.db`), `.config/gh`, `.ssh`, `.gitconfig` |
| `/workspace`   | your repositories; Claude Remote Control starts here                  |

Port `3001` is CloudCLI. Port `3002` is cli-bridge, which listens only when you
enable it. Both remote controls connect out.

Another UID works too (`user: "1000:1000"`, volumes owned by it); the image
then supplies a passwd entry through nss_wrapper. To build for another UID:
`docker build --build-arg AGENT_UID=1000 --build-arg AGENT_GID=1000 .`

### Settings

| Variable             | Default                                       |                                         |
|----------------------|-----------------------------------------------|-----------------------------------------|
| `CLAUDE_RC_DIR`      | `/workspace`                                  | where Claude Remote Control runs        |
| `CLAUDE_RC_ARGS`     | `--spawn same-dir --permission-mode default`  | replaces the default flags (split on spaces) |
| `AGENTBOX_CLAUDE_RC` | `1`                                           | `0` turns the `claude-rc` service off   |
| `AGENTBOX_CODEX_RC`  | `1`                                           | `0` turns the `codex-rc` service off    |

The container refuses to start with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_*`,
`OPENAI_API_KEY` or `CODEX_API_KEY` set: each would silently replace a
subscription login, and Remote Control needs the subscription login.

### Tailscale sidecar (optional)

To let the agents reach hosts in a tailnet (and to reach CloudCLI over it)
without giving the container any privilege, run `tailscale/tailscale` next to
it in userspace mode and share its network namespace:

```yaml
services:
  tailscale:
    image: tailscale/tailscale:stable
    hostname: agentbox
    user: "568:568"
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      TS_HOSTNAME: agentbox
      TS_USERSPACE: "true"
      TS_STATE_DIR: /var/lib/tailscale
      TS_SOCKET: /tmp/tailscaled.sock
      TS_SOCKS5_SERVER: 127.0.0.1:1055
      TS_OUTBOUND_HTTP_PROXY_LISTEN: 127.0.0.1:1055
      TS_AUTH_ONCE: "true"
      # TS_AUTHKEY: tskey-auth-...   # first start only, then remove
    ports: ["192.168.1.10:3001:3001"] # the published port moves here
    volumes:
      - /mnt/tank/apps/agentbox/tailscale:/var/lib/tailscale
  agentbox:
    network_mode: service:tailscale
    depends_on: [tailscale]
    # ...as above, without hostname and ports
```

Inside the container nothing changes for ordinary traffic. Tailnet hosts go
through the proxy on `127.0.0.1:1055`:

- `ssh` does it by itself for `*.ts.net` names and `100.64.0.0/10` addresses
  (`/etc/ssh/ssh_config.d/10-tailnet.conf`), and so does `git` over SSH
- anything else per call: `curl --proxy socks5h://127.0.0.1:1055 http://host:8080/`

Give the node a tag and let the tailnet ACL decide what it may reach; the
agents can use whatever that tag allows.

## First login

Do these in order, once. Logins survive restarts and image updates because they
live in `/home/agent`.

**1. CloudCLI account — right after the first start.** Open
`http://<server>:3001` and create the account. CloudCLI is single-user and the
first visitor becomes that user, so do not leave a fresh instance waiting on the
network.

**2. A shell.** Either CloudCLI's Shell tab, or on the server
`docker exec -it agentbox bash`.

**3. Claude.**

```sh
claude auth login           # choose the claude.ai subscription; open the URL elsewhere,
                            # sign in, paste the code it shows back here
claude auth status          # "loggedIn": true
```

Within 30 s `claude-rc` starts `claude remote-control` in tmux. Its first run
asks questions that need a terminal:

```sh
agentbox attach             # = tmux attach -t claude
```

Answer `Trust /workspace?` and `Enable Remote Control?` with `y` (both are
remembered), press space for the QR code and scan it with the Claude app, or
open claude.ai/code: the session shows up as `agentbox-…`. Detach with
`Ctrl-b d` (Ctrl-C would stop the server; `claude-rc` then restarts it).
The long-lived `claude setup-token` does not work here: it cannot open Remote
Control.

**4. Codex.**

```sh
codex login --device-auth   # open the URL elsewhere, sign in with ChatGPT, enter the code
codex login status          # exits 0 when logged in
```

A bare `codex login` — which is what CloudCLI's "Codex CLI Login" dialog runs
— is turned into the same device-code flow: the browser flow would redirect to
`127.0.0.1` on your own machine and never reach the container.

Within 30 s `codex-rc` starts the app-server with remote control. Then:

```sh
agentbox pair               # = codex remote-control pair, prints a short-lived code
```

and enter that code in the ChatGPT app. Pairing only works once the server has
registered with OpenAI, which takes a moment after the login. Do not use
`codex remote-control start|stop` here: `codex-rc` already runs the server
(`stop` says it is "not managed by codex app-server daemon", which is expected).

**5. GitHub**, with one of:

- *Fine-grained token (recommended).* github.com → Settings → Developer
  settings → Personal access tokens → Fine-grained tokens: only the
  repositories the agents should touch, *Contents* and *Pull requests*
  read/write, an expiry date. Then
  `gh auth login` (GitHub.com → HTTPS → paste a token) and `gh auth setup-git`,
  so git over HTTPS uses it.
- *Dedicated SSH key.* `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -C agentbox`,
  add `~/.ssh/id_ed25519.pub` as a **deploy key** to each repository (tick
  write access only where needed), and run `ssh -T git@github.com` once to
  record the host key (compare it with GitHub's published fingerprints). Never
  copy your personal key into the container.

Set an identity for commits: `git config --global user.name …` and
`git config --global user.email …`.

## Daily use

```sh
agentbox status             # versions, logins, services, tmux sessions
agentbox attach             # Claude Remote Control terminal (QR code: space)
agentbox pair               # new Codex pairing code
agentbox restart claude-rc  # or cloudcli, codex-rc
docker logs agentbox        # all three services log here
```

Claude Remote Control's server mode quits after about ten minutes without
network; `claude-rc` restarts it, and the sessions it served come back. A stop
of the container sends Ctrl-C first, so they stay resumable.

## cli-bridge (optional, off by default)

An OpenAI-compatible HTTP API (`POST /v1/chat/completions`, `GET /v1/models`)
that answers each request by running Claude Code (`claude -p`) or Codex
(`codex exec`) once, as a plain language model: no tools, no MCP servers,
skills, hooks, plugins or memory, in an empty directory, with a login of its
own. It is meant for one program you control on the LAN, typically an LLM
gateway. It keeps no conversation state: the caller sends the whole history
every time. Python standard library only; no added dependency.

### Terms of use: read before enabling

**Claude.** Claude Code's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance.md),
section "Authentication and credential use" (as of 2026-11-16):

> **OAuth authentication** is intended exclusively for purchasers of Claude
> Free, Pro, Max, Ultra, Team, and Enterprise subscription plans and is
> designed to support ordinary use of Claude Code and other native Anthropic
> applications.
>
> **Developers** building products or services that interact with Claude's
> capabilities, including those using the Agent SDK, should use API key
> authentication through Claude Console or a supported cloud provider.
> Anthropic does not permit third-party developers to offer Claude.ai login
> into their own applications, or to route requests through Free, Pro, Max,
> or Ultra plan credentials on behalf of their users.

and under "Acceptable use": "Advertised usage limits for Pro, Max, and Ultra
plans assume ordinary, individual usage of Claude Code and the Agent SDK."
The [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (effective
8 October 2025), which govern Free, Pro, Max and Ultra, forbid "Except when you
are accessing our Services via an Anthropic API Key or where we otherwise
explicitly permit it, to access the Services through automated or non-human
means, whether through a bot, script, or otherwise", and say you may not "make
your Account available to anyone else".

The bridge runs the unmodified Claude Code binary in print mode, which Anthropic
documents for scripts, with your own subscription. It also turns that into a
service other software calls over HTTP. Whether a personal service for one
household counts as ordinary use or as a service that should use an API key is
not settled by these documents; if other people use whatever calls the bridge,
the "make your Account available" clause matters as well. **Enabling the Claude
adapter is your decision.** The bridge never uses an API key.

**Codex.** OpenAI's Codex docs describe `codex exec` with a ChatGPT login as an
advanced option: [Non-interactive mode](https://developers.openai.com/codex/noninteractive)
lists it for "users who need ChatGPT/Codex rate limits instead of API key
usage", and [Maintain Codex account auth in CI/CD](https://developers.openai.com/codex/auth/ci-cd-auth)
says "This is an advanced workflow for enterprise and other trusted private
automation." and "Do not share the same file across concurrent jobs or multiple
machines." (hence the bridge's own Codex login, and Codex calls one at a time).
A ChatGPT login puts Codex under the ChatGPT [Terms of Use](https://openai.com/policies/terms-of-use/)
(in Switzerland, the EEA and the UK: [Europe Terms of Use](https://openai.com/policies/eu-terms-of-use/)).
Those pages refuse automated fetches, so they are not quoted here; search
results indicate a version dated 1 January 2026 whose "What you cannot do" list
covers extracting data or Output automatically or programmatically and
circumventing rate limits. Read them before enabling the Codex adapter; whether
a household service fits is, again, not settled by these documents.

### Enable

1. A token: `openssl rand -hex 32`. Set it as `AGENTBOX_CLI_BRIDGE_TOKEN`, or
   put it in a file and set `AGENTBOX_CLI_BRIDGE_TOKEN_FILE`. Without a token of
   at least 32 characters the bridge does not start (it logs why and stays down).
2. `AGENTBOX_CLI_BRIDGE=1` and at least one of `AGENTBOX_CLI_BRIDGE_CLAUDE=1`,
   `AGENTBOX_CLI_BRIDGE_CODEX=1`.
3. Publish port 3002 like 3001, on the LAN address only (with the Tailscale
   sidecar the ports belong to the sidecar):
   `ports: ["192.168.1.10:3001:3001", "192.168.1.10:3002:3002"]`.
4. Recreate the container, then log the bridge's own copies of the CLIs in.
   These are separate logins (same account, same flows as in
   [First login](#first-login)); your interactive sessions are not touched.

   ```sh
   agentbox cli-bridge login claude   # claude auth login, in the bridge's config dir
   agentbox cli-bridge login codex    # codex login --device-auth, in the bridge's CODEX_HOME
   agentbox cli-bridge status         # adapters, logins, versions, models
   agentbox cli-bridge check          # one real call per adapter (below)
   ```

| Variable                               | Default                        |                                                     |
|----------------------------------------|--------------------------------|-----------------------------------------------------|
| `AGENTBOX_CLI_BRIDGE`                  | `0`                            | `1` runs the service                                |
| `AGENTBOX_CLI_BRIDGE_TOKEN` / `_TOKEN_FILE` | –                         | bearer token, required                              |
| `AGENTBOX_CLI_BRIDGE_CLAUDE`, `_CODEX` | `0`                            | enable an adapter                                   |
| `AGENTBOX_CLI_BRIDGE_BIND`, `_PORT`    | `0.0.0.0`, `3002`              | inside the container; the published port decides who reaches it |
| `AGENTBOX_CLI_BRIDGE_CLAUDE_MODELS`    | `sonnet,opus,haiku`            | accepted after `claude/` (aliases or full model names) |
| `AGENTBOX_CLI_BRIDGE_CODEX_MODELS`     | the pinned Codex's catalog     | accepted after `codex/` (`codex debug models`, those it lists) |
| `AGENTBOX_CLI_BRIDGE_CONCURRENCY`      | `2`                            | calls at a time per adapter                         |
| `AGENTBOX_CLI_BRIDGE_CODEX_CONCURRENCY` | `1`                           | the same for Codex alone (`_CLAUDE_CONCURRENCY` likewise) |
| `AGENTBOX_CLI_BRIDGE_QUEUE`, `_QUEUE_TIMEOUT` | `4`, `30` s             | calls waiting per adapter, and for how long; then 429 |
| `AGENTBOX_CLI_BRIDGE_TIMEOUT`          | `300` s                        | per call; then the CLI's process group is killed (504) |
| `AGENTBOX_CLI_BRIDGE_MAX_REQUEST_BYTES` | `2097152`                     | larger request bodies: 413                          |
| `AGENTBOX_CLI_BRIDGE_MAX_OUTPUT_BYTES` | `1048576`                      | the answer is cut there, `finish_reason: "length"`  |
| `AGENTBOX_CLI_BRIDGE_MAX_CONNECTIONS`  | `32`                           | open connections; beyond that 503                   |
| `AGENTBOX_CLI_BRIDGE_SYSTEM_PROMPT`    | `You are a helpful assistant.` | when a request has no system message (the CLIs' own prompts are never used) |
| `AGENTBOX_CLI_BRIDGE_DEBUG`            | `0`                            | `1` also logs prompts and answers                   |

### API

| Route | |
|---|---|
| `POST /v1/chat/completions` | non-streaming, or `"stream": true` (server-sent events; `stream_options.include_usage` is honoured) |
| `GET /v1/models` | the model ids of the adapters that are enabled and logged in |
| `GET /health` | `{"status": "ok"}`; with the token also each adapter's login state, CLI version, models and queue |

The paths work without `/v1` too. Everything except `/health` needs
`Authorization: Bearer <token>`. There are no CORS headers.

**Models.** `model` is `<adapter>` (the CLI's default model) or
`<adapter>/<model>`: `claude`, `claude/sonnet`, `claude/opus`, `claude/haiku`,
`codex`, `codex/<slug>` (with Codex 0.169.0: `gpt-6.1-sol`, `gpt-6-astra`,
`gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-6.1-terra`, `gpt-5.6-terra`,
`gpt-5.6-luna`, `gpt-5.5`; which of them a ChatGPT plan allows is up to OpenAI).
Anything else is 404. `reasoning_effort` becomes `--effort` for Claude (`low`,
`medium`, `high`, `xhigh`, `max`) and `model_reasoning_effort` for Codex
(`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`).

**Messages.** `system` and `developer` messages become the system prompt
(joined in order), replacing the CLI's own. A single `user` message is the
prompt, verbatim. A longer history is written out as a transcript: one line of
instruction, then every message under a header line `--- user <b> ---` or
`--- assistant <b> ---`, where `<b>` is derived from the first message (stable
across a conversation's turns, so prompt caching still works). The last message
must come from the user. The prompt reaches the CLI on stdin and the system
prompt as a file, never through a shell.

**Parameters.** Accepted but without effect, and named in the response header
`x-cli-bridge-ignored`: `temperature`, `top_p`, `max_tokens`,
`max_completion_tokens`, `presence_penalty`, `frequency_penalty`, `seed`,
`logit_bias`, `user`, `store`, `metadata`, `service_tier`, and any parameter
this version does not know. Refused with 400 `unsupported_parameter`, never
dropped: tools and tool choices (`tools`, `tool_choice`, `functions`,
`function_call`, tool messages, assistant tool calls), images, audio and files
in content, `response_format` other than `text`, `n` > 1, `logprobs`,
`top_logprobs`, `audio`, `modalities` other than text, `prediction`,
`web_search_options`, `stop`.

**Errors** come in the OpenAI format (`{"error": {"message", "type", "param", "code"}}`):

| Status | `code` | When |
|---|---|---|
| 400 | `unsupported_parameter`, `invalid_request`, `invalid_json` | see above |
| 401 | `invalid_api_key` | bearer token missing or wrong |
| 401 | `not_logged_in` | the bridge's CLI login is missing or was rejected |
| 403 | `plan_not_included`, `account_on_hold`, … | the account may not use it |
| 404 | `model_not_found` | unknown model, or the adapter is disabled |
| 413 | `request_too_large` | body over `MAX_REQUEST_BYTES` |
| 429 | `usage_limit_reached`, `insufficient_quota`, `rate_limit_exceeded` | a subscription limit; Claude's carries `Retry-After` until the reset |
| 429 | `queue_full`, `queue_timeout` | every slot busy and the queue full (`Retry-After: 5`) |
| 502 | `cli_failed`, `isolation_violation`, `output_too_large` | the CLI failed, or tried to use a tool (the call is stopped) |
| 503 | `overloaded` | the provider is at capacity |
| 504 | `cli_timeout` | over `TIMEOUT`; the process group was killed |

Streaming sends its response headers with the first piece of the answer, so an
error before that gets its status code too; an error later arrives as a last
`data: {"error": …}` event, without `[DONE]`. Claude streams token by token;
Codex delivers its answer in one piece (`codex exec --json` reports only
completed messages). A client that hangs up stops the CLI.

### How the CLIs run

Each call: an argument vector (no shell), its own process group, the prompt on
stdin, the working directory `/tmp/agentbox/cli-bridge/work` (empty; anything
left there is removed and logged), `HOME=/tmp/agentbox/cli-bridge/home`, a
per-call `TMPDIR` that is deleted afterwards, and an environment built from an
allowlist (PATH, locale, proxy and CA settings, nss_wrapper) plus the adapter's
own variables. `ANTHROPIC_*`, `OPENAI_*`, `CODEX_API_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN` are never passed: an `ANTHROPIC_API_KEY` alone would
silently move Claude to API billing.

Claude Code, checked against 2.1.324 (`claude --help` and its option table) and
the [CLI reference](https://code.claude.com/docs/en/cli-reference.md),
[headless](https://code.claude.com/docs/en/headless.md) and
[environment variables](https://code.claude.com/docs/en/env-vars.md) pages:

```
claude -p --output-format stream-json --verbose --include-partial-messages
  --no-session-persistence --safe-mode --setting-sources "" --strict-mcp-config
  --disable-slash-commands --permission-mode dontAsk --max-turns 1
  --system-prompt-file <per-call file> [--model M] [--effort E]
  --tools "" --disallowedTools "*"
CLAUDE_CONFIG_DIR=~/.local/state/agentbox/cli-bridge/claude  CLAUDE_CODE_TMPDIR=<per call>
CLAUDE_CODE_SKIP_PROMPT_HISTORY=1  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1  CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING=1  CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1
ENABLE_CLAUDEAI_MCP_SERVERS=false
```

`--safe-mode` drops CLAUDE.md, skills, plugins, hooks, MCP servers, custom
commands and agents, output styles and auto memory while keeping the
subscription login; `--setting-sources ""` loads no user, project or local
settings (managed settings still apply). `--max-turns` and
`--system-prompt-file` are hidden from `--help` but are in the option table and
the CLI reference. `--bare` cannot be used: it never reads OAuth or keychain
credentials. If Claude Code's start event lists any tool or MCP server, the
call is stopped.

Codex, checked against 0.169.0 (`codex exec --help`, `codex features list`,
`codex debug prompt-input`, its source at tag `rust-v0.169.0`) and, in CI, by
running the real binary against a fake Responses API:

```
codex exec --json --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules
  --sandbox read-only --color never
  -c approval_policy="never" -c history.persistence="none" -c check_for_update_on_startup=false
  -c web_search="disabled" -c project_doc_max_bytes=0
  -c skills.bundled.enabled=false -c skills.include_instructions=false
  -c include_permissions_instructions=false -c include_apps_instructions=false
  -c include_environment_context=false -c include_collaboration_mode_instructions=false
  -c agents.enabled=false -c tools.update_plan.enabled=false
  -c tools.experimental_request_user_input.enabled=false
  -c model_instructions_file="<per-call file>"
  --disable shell_tool --disable unified_exec --disable shell_snapshot --disable multi_agent
  --disable apps --disable plugins --disable hooks --disable memories --disable view_image
  --disable image_generation --disable browser_use --disable browser_use_external
  --disable computer_use --disable goals --disable tool_suggest --disable skill_search
  --disable sleep_tool [--model M] [-c model_reasoning_effort="E"] -
CODEX_HOME=~/.local/state/agentbox/cli-bridge/codex
```

The model then sees the system prompt (as a developer message) and the prompt,
nothing else: no environment context, AGENTS.md, skills or collaboration
instructions. Two things 0.169.0 cannot switch off, because the model catalog
adds them: the `apply_patch` tool, and for models marked `code_mode_only` (the
gpt-6 and gpt-5.6 families) a JavaScript `exec` cell (a V8 isolate without file
system or network). With a read-only sandbox and `approval_policy="never"`
Codex rejects every patch before applying it (`assess_patch_safety` in its
source), and any tool item in the event stream stops the call. Codex always reads a global `AGENTS.md` from
its CODEX_HOME; the bridge's own CODEX_HOME has none. The image's
`/etc/codex/config.toml` is still read; the flags above override what matters.

### What a call writes

Only the bridge's own directories: `~/.local/state/agentbox/cli-bridge/`
(persistent: the two logins and CLI state) and `/tmp/agentbox/cli-bridge/`
(tmpfs). Nothing in `/workspace`, in your own `~/.claude` or `~/.codex`, or in
CloudCLI's project list. The smoke test checks this with the stubs and with the
real CLIs (Claude without a login, Codex against the fake API).

- **Claude Code**: no transcript (`projects/…/<session>.jsonl`), no
  `history.jsonl`, no auto memory, no file history (see the
  [.claude directory](https://code.claude.com/docs/en/claude-directory.md)
  docs). Expected in its config directory: `.claude.json` (app state, one
  project entry for the work directory) with up to five copies in `backups/`,
  `.credentials.json` (the login; refreshed tokens), small caches
  (`remote-settings.json`, `policy-limits.json`, `stats-cache.json`, `cache/`),
  and a file in `sessions/` while a call runs. *Not verified with a real
  login*; `agentbox cli-bridge check` shows it.
- **Codex**: no rollout in `sessions/` (`--ephemeral`), no `history.jsonl`.
  Seen with the real 0.169.0: `installation_id` and the SQLite databases
  `state_5`, `logs_2`, `queue_1`, `memories_1`, `goals_1` (with their `-wal`
  and `-shm` files), which held neither prompt nor answer; with a login also
  `auth.json` and probably `models_cache.json`.

### Check with the real logins

```sh
agentbox cli-bridge check                 # each enabled adapter's default model
agentbox cli-bridge check claude/haiku codex/gpt-5.5
```

For each model one non-streaming and one streaming request ("Reply with exactly
one word: pong") through the running service. It prints status, total time,
time to the first text, token counts and the answer, then every file written
meanwhile under `$HOME`, `/workspace` and `/tmp`, with `!` in front of any
outside the bridge's directories. It uses a little of your quota.

## Security model

What the container does:

- runs as UID 568 (or the UID you give it), never root; init refuses UID 0
- no sudo, no setuid/setgid files; works with `no-new-privileges`,
  `cap_drop: ALL` and a read-only root filesystem; no Docker socket
- only the two volumes are writable; the tools under `/opt/agentbox` are
  root-owned, so no agent can replace them
- Claude: managed settings (`/etc/claude-code/managed-settings.json`) force the
  claude.ai login and **disable bypass-permissions mode**: every tool call that
  needs approval is asked for, on the phone or in CloudCLI
- no listening port except CloudCLI and, only when you enable it,
  cli-bridge (bearer token compared in constant time, no CORS, request and
  answer size caps, a bounded queue, a hard timeout per call); the remote
  controls connect out to Anthropic and OpenAI over TLS
- cli-bridge runs the CLIs without tools, with logins and state of their own,
  never with an API key in their environment, and logs one line per request
  without prompt or answer

What it does not do — the limits:

- **Every agent can read every credential in `/home/agent`**: Claude's and
  Codex's OAuth tokens, the GitHub token or key, CloudCLI's database. A
  prompt-injected agent whose command you approve can send them anywhere: the
  network is not restricted. Approve with care, keep the GitHub access narrow
  and time-limited, and revoke logins if in doubt.
- **Codex runs without its own sandbox.** Codex sandboxes commands with
  bubblewrap, which needs user namespaces that Docker's default seccomp profile
  forbids. `/etc/codex/config.toml` therefore sets `sandbox_mode =
  "danger-full-access"` with `approval_policy = "on-request"`: Codex itself
  decides when to ask, which in practice means it rarely does. Treat Codex
  sessions as unattended within this container. (Allowing user namespaces
  would need a relaxed seccomp profile for the whole container; agentbox does
  not do that.)
- **CloudCLI is a remote shell** for whoever holds its password, served over
  plain HTTP. It had three critical advisories in March 2026, among them an
  unauthenticated RCE through its WebSocket shell (fixed in 1.24/1.25; this
  image pins a current release). Bind port 3001 to the LAN address, never
  forward it, use a VPN from outside, and put TLS in front if the LAN is not
  trusted. CloudCLI's plugin installer (`git clone` of any repository) and its
  "update" button are part of that surface; the update fails here by design.
- **Browser Use gives the agents a real browser.** The image carries
  Playwright and Chromium so CloudCLI's Browser tab works (its own installer
  cannot run here: no root, read-only filesystem). Chromium runs headless,
  without its own sandbox (as in any unprivileged container), with the same
  network reach as the agents, and it executes whatever a page serves. Saved
  browser profiles live in `~/.cloudcli/browser-use/` and hold cookies.
- **cli-bridge, when enabled, spends your subscriptions for whoever holds its
  token.** Anyone who can reach port 3002 and knows the token uses your Claude
  and ChatGPT quota; the token is the only check, sent over plain HTTP, so keep
  the port on a trusted LAN address (or put TLS in front) and the token secret.
  With the Tailscale sidecar, tailnet devices the ACL lets reach the node can
  reach 3002 too. The agents in the container can read the token and the
  bridge's logins like every other credential in `/home/agent`. Without tools
  the CLIs can only answer: a prompt injection changes the answer, not the
  machine. Codex keeps two tools the bridge cannot remove (see
  [How the CLIs run](#how-the-clis-run)); it rejects every patch in its
  read-only mode, and any tool use stops the call. The terms questions are in
  [Terms of use](#terms-of-use-read-before-enabling).
- While Remote Control is on, conversation transcripts are stored by Anthropic
  and OpenAI respectively.
- The image redistributes Claude Code (Anthropic's commercial terms), Codex
  (Apache-2.0) and CloudCLI (AGPL-3.0-or-later), unmodified.

## Updates

Nothing updates itself inside the container: `DISABLE_AUTOUPDATER=1` and
`DISABLE_UPDATES=1` for Claude (also in the managed settings), Codex's update
check is off, the install tree is read-only for the agent user. The image is
the single source of truth:

1. **Dependabot** checks weekly: the three CLIs (`package.json` with exact
   versions + lockfile; patch and minor updates come as one grouped PR, once
   a release is three days old), the
   Node base image tag and digest (new Node majors are ignored, the next LTS is
   a deliberate change), and the GitHub Actions (pinned by commit SHA).
   Dependabot security updates are on as well; their PRs for vulnerable
   transitive npm packages take the same path.
2. **CI** on the PR: hadolint, shellcheck, image build, `test/smoke.sh`, and a
   Grype scan that fails on fixable critical findings and lists the rest in the
   run summary. Accept a finding by adding it to a `.grype.yaml` ignore list.
3. **Auto-merge**: when every check passed, the `automerge` job squash-merges
   Dependabot's patch and minor PRs and starts the release. Majors stay open
   for a human.
4. **Release** (`release.yaml`) reruns the same checks on `main`, then
   publishes `ghcr.io/pburkhalter/agentbox` for linux/amd64 with SBOM and
   provenance attestations. Tags: `latest`, `YYYY.MM.DD-<sha>`, and for a git
   tag `vX.Y.Z` also `vX.Y.Z` and `vX.Y`.
5. **Weekly rebuild** (Monday 03:23 UTC) without build cache, for Debian
   security updates and the current gh, even when no dependency changed.

The running container does not follow `latest` by itself. Pull and recreate it
on whatever schedule suits you (restarts interrupt running agent work), or pin a
date tag and move it deliberately. Verify an image with
`gh attestation verify oci://ghcr.io/pburkhalter/agentbox:latest --owner pburkhalter`.

## Develop

```sh
docker build -t agentbox:dev .
test/smoke.sh agentbox:dev   # needs docker, curl, jq; ~3 min
python3 -m unittest discover -s test/unit   # cli-bridge alone, no docker; ~10 s
```

`test/smoke.sh` starts the image with empty volumes, a read-only root and no
capabilities, and checks: non-root everywhere, no sudo or setuid files, the
pinned CLI versions, CloudCLI on 3001 (inside and through a published port),
fresh-volume initialisation and persistence across a restart, no API key in any
environment, self-update refused, both agents idling without crash loops before
login, graceful stop, another UID, the refusals, and — with stub `claude` and
`codex` that report a login — the tmux session, its flags, restart after exit
and the Codex server command line. For cli-bridge: off by default (nothing
listens, no state), refusal without a token, the token check, `/v1/models`,
non-streaming and streaming answers from both adapters, every error mapping,
the output cap, timeout with the process group killed, a client hanging up,
the full queue, shell metacharacters arriving as literal text, the environment
and flags the CLIs get, that no file is written outside the bridge's
directories and no prompt or answer reaches the log; then the real, pinned
`claude` (without a login: 401, so every flag is accepted) and `codex` (against
`test/fake_responses.py`: answer, usage, and exactly what it sent the model),
and the unit tests with the image's Python. The stubs answer like the CLIs'
JSON event streams; keywords in the prompt pick the case (`test/stubs/stub_cli.py`).

Layout: `Dockerfile`, `package.json`/`package-lock.json` (the CLIs),
`rootfs/` (copied into the image: init, services, policy files, cli-bridge in
`rootfs/usr/local/lib/agentbox/cli-bridge/`), `test/` (`smoke.sh`, `stubs/`,
`unit/`). A cli-bridge adapter for another CLI is one module in
`cli_bridge/adapters/` (see `base.py`); it is enabled with
`AGENTBOX_CLI_BRIDGE_<NAME>=1`.

## License

MIT
