# syntax=docker/dockerfile:1

# Node LTS on Debian 13. Tag and digest are pinned; Dependabot bumps both.
FROM node:26.13.1-trixie-slim@sha256:e6bedef217677b276c591795e169840edd9096dbf13f29df8ef8d1856b8820bc AS base

# ---------------------------------------------------------------------------
# The three CLIs, installed from package-lock.json. Compilers for CloudCLI's
# native modules (node-pty, better-sqlite3, bcrypt) stay in this stage.
FROM base AS tools
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/agentbox
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund --no-update-notifier \
 && npm cache clean --force \
 # Unused here: the Agent SDK's own claude binary (CloudCLI is pointed at the
 # pinned CLI via CLAUDE_CLI_PATH), musl builds (this is glibc) and the
 # desktop-automation helpers (no display in a container).
 && rm -rf node_modules/@anthropic-ai/claude-agent-sdk-linux-* \
           node_modules/@anthropic-ai/claude-code-linux-x64-musl \
           node_modules/@nut-tree-fork \
           node_modules/screenshot-desktop \
 # Fails the build when an install script did not place a native binary.
 && node_modules/.bin/claude --version \
 && node_modules/.bin/codex --version \
 && node_modules/.bin/cloudcli --version

# ---------------------------------------------------------------------------
FROM base

ARG AGENT_UID=568
ARG AGENT_GID=568
# GitHub CLI apt repository key. A rotation fails the build here on purpose.
ARG GH_KEYRING_SHA256=6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Distribution packages are not version-pinned: the weekly rebuild picks up
# Debian security updates (apt-get upgrade) and the current gh release.
# hadolint ignore=DL3008,DL3005
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less libnss-wrapper netcat-openbsd \
      openssh-client procps python3 ripgrep s6 tmux \
 && curl -fsSLo /usr/share/keyrings/githubcli-archive-keyring.gpg \
      https://cli.github.com/packages/githubcli-archive-keyring.gpg \
 && echo "${GH_KEYRING_SHA256}  /usr/share/keyrings/githubcli-archive-keyring.gpg" | sha256sum -c - \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 # No setuid/setgid binaries: nothing in the image can raise privileges, even
 # without no-new-privileges.
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
 # The base image's node user would take UID 1000; agent gets its own IDs.
 && userdel -r node \
 && groupadd -g "${AGENT_GID}" agent \
 && useradd -l -u "${AGENT_UID}" -g agent -d /home/agent -m -s /bin/bash agent \
 && install -d -o agent -g agent -m 0755 /workspace \
 && chmod 0700 /home/agent \
 # Interactive shells pick up the nss_wrapper variables init writes for a
 # UID that is not in /etc/passwd (see lib.sh).
 && echo '[ -r /tmp/agentbox/nss.env ] && . /tmp/agentbox/nss.env' >>/etc/bash.bashrc

COPY --from=tools /opt/agentbox /opt/agentbox
COPY rootfs/ /

ENV HOME=/home/agent \
    PATH=/opt/agentbox/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    CODEX_HOME=/home/agent/.codex \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

LABEL org.opencontainers.image.title="agentbox" \
      org.opencontainers.image.description="Claude Code, Codex and CloudCLI as a supervised, non-root, always-on container" \
      org.opencontainers.image.source="https://github.com/pburkhalter/agentbox" \
      org.opencontainers.image.licenses="MIT"

USER ${AGENT_UID}:${AGENT_GID}
WORKDIR /workspace
VOLUME ["/home/agent", "/workspace"]
EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/usr/local/lib/agentbox/healthcheck"]
ENTRYPOINT ["/usr/local/lib/agentbox/init"]
