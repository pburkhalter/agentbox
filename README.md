# agentbox

A container that keeps AI coding agents running on a home server: Claude Code
and OpenAI Codex, both reachable from the phone through their own remote
control, plus [CloudCLI](https://github.com/siteboon/claudecodeui) as a web UI
for both. It runs as a normal user, holds no credentials until you log in, and
updates by being rebuilt, never by the tools updating themselves.

```
phone   Claude app  ⇄ Anthropic ⇠ claude-rc  claude remote-control  (tmux "claude")
phone   ChatGPT app ⇄ OpenAI    ⇠ codex-rc   codex app-server --remote-control
browser (LAN / VPN) ───── :3001 → cloudcli   CloudCLI: chat, shell, files, git

s6-svscan (PID 1) supervises the three services. The remote controls only
connect out. State: /home/agent (logins, tool config), /workspace (repos).
```

| Service     | What runs                                                        | Before you log in        |
|-------------|------------------------------------------------------------------|--------------------------|
| `cloudcli`  | `cloudcli start --port 3001`                                     | runs                     |
| `claude-rc` | `claude remote-control --spawn same-dir --permission-mode default` in `/workspace`, inside tmux session `claude` | idles, checks every 30 s |
| `codex-rc`  | `codex app-server --remote-control --listen unix://` (the server `codex remote-control start` would launch) | idles, checks every 30 s |

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

Port `3001` is CloudCLI. Nothing else listens; both remote controls connect
out.

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
- no listening port except CloudCLI; the remote controls connect out to
  Anthropic and OpenAI over TLS

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
test/smoke.sh agentbox:dev   # needs docker, curl, jq; ~2 min
```

`test/smoke.sh` starts the image with empty volumes, a read-only root and no
capabilities, and checks: non-root everywhere, no sudo or setuid files, the
pinned CLI versions, CloudCLI on 3001 (inside and through a published port),
fresh-volume initialisation and persistence across a restart, no API key in any
environment, self-update refused, both agents idling without crash loops before
login, graceful stop, another UID, the refusals, and — with stub `claude` and
`codex` that report a login — the tmux session, its flags, restart after exit
and the Codex server command line.

Layout: `Dockerfile`, `package.json`/`package-lock.json` (the CLIs),
`rootfs/` (copied into the image: init, services, policy files), `test/`.

## License

MIT
