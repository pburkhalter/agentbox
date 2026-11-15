#!/usr/bin/env bash
# Starts the image the way the NAS runs it and checks everything that must
# hold before anyone has logged in. Needs docker, curl and jq on the host.
#
#   test/smoke.sh IMAGE
#
# Commands in single quotes run inside the container, not here.
# shellcheck disable=SC2016
set -euo pipefail

image=${1:?usage: test/smoke.sh IMAGE}
repo=$(cd "$(dirname "$0")/.." && pwd)
run_id=$$-$RANDOM
c=agentbox-smoke-$run_id
home_vol=agentbox-smoke-home-$run_id
ws_vol=agentbox-smoke-ws-$run_id
failures=0

cleanup() {
	docker rm -f "$c" "$c-vol" "$c-uid" "$c-stub" >/dev/null 2>&1 || true
	docker volume rm "$home_vol" "$ws_vol" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ok() { printf 'ok   %s\n' "$1"; }
bad() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}
# check DESCRIPTION CMD...: passes when CMD exits 0
check() {
	local desc=$1
	shift
	if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
in_c() { docker exec "$c" bash -c "$1"; }
pinned() { jq -r --arg p "$1" '.dependencies[$p]' "$repo/package.json"; }

wait_healthy() {
	local name=$1 status
	for _ in $(seq 60); do
		status=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo gone)
		[ "$status" = healthy ] && return 0
		[ "$status" = gone ] && return 1
		sleep 2
	done
	return 1
}

# Same hardening as compose.example.yaml.
hardening=(--security-opt no-new-privileges --cap-drop ALL)

echo "== image config"
cfg=$(docker image inspect "$image")
env_list=$(jq -r '.[0].Config.Env[]' <<<"$cfg")
key_vars='^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|OPENAI_API_KEY|CODEX_API_KEY)='
check "image user is 568:568" test "$(jq -r '.[0].Config.User' <<<"$cfg")" = 568:568
check "image sets DISABLE_AUTOUPDATER=1" grep -qx DISABLE_AUTOUPDATER=1 <<<"$env_list"
check "image sets DISABLE_UPDATES=1" grep -qx DISABLE_UPDATES=1 <<<"$env_list"
check "image sets no API key or token variable" bash -c "! grep -Eq '$key_vars'" <<<"$env_list"
check "image has a healthcheck" jq -e '.[0].Config.Healthcheck.Test' <<<"$cfg"

echo "== fresh, empty volumes"
# tmpfs mounts are truly empty, like a new NAS dataset owned by the apps user.
# The strictest setup: read-only root, /tmp a default (noexec) tmpfs.
docker run -d --name "$c" "${hardening[@]}" --read-only --tmpfs /tmp -p 127.0.0.1::3001 \
	--tmpfs /home/agent:uid=568,gid=568,mode=0755 \
	--tmpfs /workspace:uid=568,gid=568,mode=0755 \
	"$image" >/dev/null
if wait_healthy "$c"; then ok "container becomes healthy"; else
	bad "container becomes healthy"
	docker logs "$c" 2>&1 | tail -40
	exit 1
fi

check "runs as uid 568, gid 568" test "$(in_c 'echo "$(id -u):$(id -g)"')" = 568:568
check "no process runs as root" test "$(in_c 'ps -eo uid= | sort -u | tr -d " "')" = 568
check "PID 1 is s6-svscan" test "$(in_c 'cat /proc/1/comm')" = s6-svscan
check "no sudo in the image" in_c '! command -v sudo'
check "no setuid/setgid files" test -z "$(in_c 'find / -xdev -type f -perm /6000 2>/dev/null')"

check "claude prints the pinned version" \
	bash -c "docker exec $c claude --version | grep -q '^$(pinned @anthropic-ai/claude-code) '"
check "codex prints the pinned version" \
	test "$(in_c 'codex --version')" = "codex-cli $(pinned @openai/codex)"
check "cloudcli prints the pinned version" test "$(in_c 'cloudcli --version')" = "$(pinned @cloudcli-ai/cloudcli)"
check "CloudCLI resolves the same codex" test "$(in_c "node -p \"require(require.resolve('@openai/codex/package.json', {paths: ['/opt/agentbox/node_modules/@cloudcli-ai/cloudcli']})).version\"")" = "$(pinned @openai/codex)"
check "git, gh, ssh, tmux, rg, jq, curl, python3 present" \
	in_c 'git --version && gh --version && ssh -V && tmux -V && rg --version && jq --version && curl --version && python3 --version'
check "ssh sends tailnet hosts through the sidecar proxy, others direct" in_c \
	'command -v nc && ssh -G 100.68.1.2 | grep -qx "proxycommand nc -X 5 -x 127.0.0.1:1055 %h %p" && ssh -G box.example.ts.net | grep -q "^proxycommand nc" && ! ssh -G github.com | grep -q "^proxycommand" && ssh -G 100.127.9.9 | grep -q "^proxycommand nc" && ! ssh -G 100.63.2.3 | grep -q "^proxycommand" && ! ssh -G 100.128.2.3 | grep -q "^proxycommand"'
check "node is the LTS line 26" bash -c "docker exec $c node --version | grep -q '^v26\.'"

health=$(in_c 'curl -fsS http://127.0.0.1:3001/health' || true)
check "CloudCLI answers /health on 3001" bash -c "jq -e '.status == \"ok\"' <<<'$health'"
port=$(docker port "$c" 3001/tcp | head -1 | sed 's/.*://')
check "CloudCLI is reachable through the published port" curl -fsS "http://127.0.0.1:$port/health"
check "CloudCLI wants its first account (fresh database)" \
	bash -c "docker exec $c curl -fsS http://127.0.0.1:3001/api/auth/status | jq -e '.needsSetup == true'"

check "home volume initialised (marker, config dirs)" in_c \
	'test -s ~/.local/state/agentbox/initialized && test -d "$CLAUDE_CONFIG_DIR" && test -d "$CODEX_HOME" && test -f ~/.cloudcli/auth.db && test -f ~/.bashrc'
check "home and ~/.ssh are private (0700)" test "$(in_c 'stat -c %a ~ ~/.ssh | sort -u')" = 700
check "init logged the first start" bash -c "docker logs $c 2>&1 | grep -q 'initialised a fresh home volume'"

check "no API key in the environment of PID 1 or CloudCLI" in_c \
	'for p in 1 $(pgrep -f "cloudcli start"); do ! tr "\0" "\n" </proc/$p/environ | grep -Eq "^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|OPENAI_API_KEY)="; done'
check "claude refuses to update itself" bash -c "docker exec $c claude update 2>&1 | grep -q 'Updates are disabled'"
check "managed settings force claude.ai login and disable bypass mode" in_c \
	'jq -e ".forceLoginMethod == \"claudeai\" and .permissions.disableBypassPermissionsMode == \"disable\"" /etc/claude-code/managed-settings.json'
check "codex update check is off and no daemon auto-start" in_c \
	'grep -qx "check_for_update_on_startup = false" /etc/codex/config.toml && codex features list | grep -Eq "^daemon_auto_start +[a-z]+ +false$"'
check "CLI install tree and npm's global prefix are not writable (no self-update)" in_c \
	'! test -w /opt/agentbox/node_modules/.bin && ! test -w /usr/local/lib/node_modules'
check "codex app-server accepts --remote-control (used by codex-rc)" in_c \
	'codex app-server --remote-control --listen unix:///tmp/smoke.sock >/dev/null 2>&1 & p=$!; sleep 4; kill -0 $p && kill $p'

# The agents idle instead of crash-looping while nobody is logged in.
sleep 10
for s in claude-rc codex-rc cloudcli; do
	check "service $s is up and has not restarted" \
		bash -c "docker exec $c s6-svstat -o up,updownfor /tmp/agentbox/service/$s | awk '\$1 == \"true\" && \$2 >= 20 { found = 1 } END { exit !found }'"
done
check "claude-rc and codex-rc each logged 'not logged in' once" \
	test "$(docker logs "$c" 2>&1 | grep -c 'is not logged in; idle')" = 2

start=$(date +%s)
docker stop -t 30 "$c" >/dev/null
check "docker stop is graceful (exit 0, under 15 s)" \
	bash -c "test \$(docker inspect -f '{{.State.ExitCode}}' $c) = 0 && test $(($(date +%s) - start)) -lt 15"

echo "== named volumes survive a restart"
docker volume create "$home_vol" >/dev/null
docker volume create "$ws_vol" >/dev/null
docker run -d --name "$c-vol" "${hardening[@]}" -v "$home_vol:/home/agent" -v "$ws_vol:/workspace" "$image" >/dev/null
wait_healthy "$c-vol" || bad "volume container becomes healthy"
check "first CloudCLI account can be created" bash -c "docker exec $c-vol curl -fsS -H 'Content-Type: application/json' \
	-d '{\"username\":\"smoke\",\"password\":\"smoke-test-only\"}' http://127.0.0.1:3001/api/auth/register | jq -e .success"
marker=$(docker exec "$c-vol" cat /home/agent/.local/state/agentbox/initialized)
docker restart -t 30 "$c-vol" >/dev/null
wait_healthy "$c-vol" || bad "volume container healthy after restart"
check "CloudCLI account survives the restart" \
	bash -c "docker exec $c-vol curl -fsS http://127.0.0.1:3001/api/auth/status | jq -e '.needsSetup == false'"
check "init marker unchanged after restart" test "$(docker exec "$c-vol" cat /home/agent/.local/state/agentbox/initialized)" = "$marker"

echo "== another UID"
docker run -d --name "$c-uid" "${hardening[@]}" --user 1000:1000 \
	--tmpfs /home/agent:uid=1000,gid=1000,mode=0755 \
	--tmpfs /workspace:uid=1000,gid=1000,mode=0755 \
	"$image" >/dev/null
if wait_healthy "$c-uid"; then ok "UID 1000 container becomes healthy"; else bad "UID 1000 container becomes healthy"; fi
check "UID 1000 has a name in interactive shells (nss_wrapper)" \
	test "$(docker exec "$c-uid" bash -ic whoami 2>/dev/null)" = agent
check "ssh works for UID 1000" docker exec "$c-uid" bash -ic 'ssh -G example.com'

echo "== supervision after login (stub CLIs)"
# The real logins cannot happen in CI. Stubs that report "logged in" drive
# the real service scripts: tmux session, arguments, restart, stop.
docker run -d --name "$c-stub" "${hardening[@]}" \
	-v "$repo/test/stubs:/stubs:ro" -e PATH="/stubs:$(jq -r '.[0].Config.Env[] | select(startswith("PATH=")) | ltrimstr("PATH=")' <<<"$cfg")" \
	--tmpfs /home/agent:uid=568,gid=568 --tmpfs /workspace:uid=568,gid=568 \
	"$image" >/dev/null
wait_healthy "$c-stub" || bad "stub container becomes healthy"
wait_for() {
	for _ in $(seq 30); do
		"$@" >/dev/null 2>&1 && return 0
		sleep 1
	done
	return 1
}
check "claude-rc opens tmux session 'claude'" wait_for docker exec "$c-stub" tmux has-session -t =claude
check "remote-control runs in /workspace with spawn same-dir, default permission mode" \
	wait_for bash -c "docker exec $c-stub tmux capture-pane -p -t =claude: | grep -qF 'stub claude remote-control --spawn same-dir --permission-mode default (cwd /workspace)'"
check "its first-run prompt waits in the session" bash -c "docker exec $c-stub tmux capture-pane -p -t =claude: | grep -q 'Enable Remote Control'"
docker exec "$c-stub" tmux send-keys -t =claude: y Enter
check "an exit is logged with its status" \
	wait_for bash -c "docker logs $c-stub 2>&1 | grep -q 'claude remote-control ended (exit status 3); restarting'"
check "and a new session is started" \
	wait_for bash -c "docker exec $c-stub tmux capture-pane -p -t =claude: | grep -q 'Enable Remote Control'"
check "codex-rc runs the app-server with remote control on the default socket" \
	wait_for bash -c "docker logs $c-stub 2>&1 | grep -qF 'stub codex app-server --remote-control --listen unix://'"
start=$(date +%s)
docker stop -t 30 "$c-stub" >/dev/null
check "stop ends the tmux session gracefully (exit 0, under 15 s)" \
	bash -c "test \$(docker inspect -f '{{.State.ExitCode}}' $c-stub) = 0 && test $(($(date +%s) - start)) -lt 15"

echo "== refusals"
# refuses DESCRIPTION PATTERN DOCKER-RUN-ARGS...: the container must exit
# non-zero and say PATTERN.
refuses() {
	local desc=$1 pattern=$2 out rc=0
	shift 2
	out=$(timeout 60 docker run --rm "$@" "$image" 2>&1) || rc=$?
	if [ "$rc" -ne 0 ] && grep -q "$pattern" <<<"$out"; then ok "$desc"; else bad "$desc"; fi
}
refuses "refuses to start with ANTHROPIC_API_KEY set" "ANTHROPIC_API_KEY is set" \
	"${hardening[@]}" --tmpfs /home/agent:uid=568,gid=568 -e ANTHROPIC_API_KEY=sk-test
refuses "refuses to start with OPENAI_API_KEY set" "OPENAI_API_KEY is set" \
	"${hardening[@]}" --tmpfs /home/agent:uid=568,gid=568 -e OPENAI_API_KEY=sk-test
refuses "refuses to run as root" "refusing to run as root" --user 0
refuses "refuses a home it cannot write" "is not writable" \
	"${hardening[@]}" --tmpfs /home/agent:uid=0,gid=0,mode=0755

echo
if [ "$failures" -gt 0 ]; then
	echo "$failures check(s) failed"
	exit 1
fi
echo "all checks passed"
