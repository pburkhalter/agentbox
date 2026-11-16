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

work=$(mktemp -d)

cleanup() {
	docker rm -f "$c" "$c-vol" "$c-uid" "$c-stub" "$c-bridge" "$c-notoken" >/dev/null 2>&1 || true
	docker volume rm "$home_vol" "$ws_vol" >/dev/null 2>&1 || true
	rm -rf "$work"
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
in_c_b() { docker exec "$c-bridge" bash -c "$1"; }
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
check "bare 'codex login' becomes the device-code flow, everything else passes through" in_c \
	'export AGENTBOX_CODEX_BIN=/bin/echo; test "$(codex login)" = "login --device-auth" && test "$(codex login --with-api-key)" = "login --with-api-key" && test "$(codex login status)" = "login status" && test "$(codex exec "a b")" = "exec a b"'
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

# CloudCLI's Browser Use: the same launch options it uses, in the hardened
# container (read-only root, no capabilities, non-root).
check "Chromium starts headless and loads a page (Browser Use runtime)" in_c \
	'cd /opt/agentbox && node -e "
const { chromium } = require(\"playwright\");
(async () => {
  if (!require(\"fs\").existsSync(chromium.executablePath())) throw new Error(\"no chromium\");
  const b = await chromium.launch({ headless: true, args: [\"--disable-dev-shm-usage\"] });
  const p = await b.newPage();
  await p.goto(\"http://127.0.0.1:3001/health\");
  const t = await p.textContent(\"body\");
  await b.close();
  if (!t.includes(\"ok\")) throw new Error(t);
})().catch((e) => { console.error(e); process.exit(1); });"'

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
check "cli-bridge is down by default" \
	bash -c "docker exec $c s6-svstat -o up /tmp/agentbox/service/cli-bridge | grep -qx false"
check "nothing listens on 3002 by default" in_c '! curl -s -o /dev/null --max-time 2 http://127.0.0.1:3002/health'
check "the disabled bridge created no state" in_c '! test -e ~/.local/state/agentbox/cli-bridge && ! test -e /tmp/agentbox/cli-bridge'

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
image_path=$(jq -r '.[0].Config.Env[] | select(startswith("PATH=")) | ltrimstr("PATH=")' <<<"$cfg")
docker run -d --name "$c-stub" "${hardening[@]}" \
	-v "$repo/test/stubs:/stubs:ro" -e PATH="/stubs:$image_path" \
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

echo "== cli-bridge (stub CLIs)"
# The real logins cannot happen in CI. Stub claude/codex (test/stubs) answer
# like the real CLIs' JSON event streams; keywords in the prompt pick the
# behaviour. The real CLIs run at the end: Claude without a login, Codex
# against a fake Responses API (test/fake_responses.py).
b=$c-bridge
token=smoke-$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
docker run -d --name "$b" "${hardening[@]}" --read-only --tmpfs /tmp -p 127.0.0.1::3002 \
	-v "$repo/test:/test:ro" -v "$repo/test/fixtures/codex-config.toml:/etc/codex/config.toml:ro" \
	-e PATH="/test/stubs:$image_path" -e AGENTBOX_CLAUDE_RC=0 -e AGENTBOX_CODEX_RC=0 \
	-e AGENTBOX_CLI_BRIDGE=1 -e AGENTBOX_CLI_BRIDGE_TOKEN="$token" \
	-e AGENTBOX_CLI_BRIDGE_CLAUDE=1 -e AGENTBOX_CLI_BRIDGE_CODEX=1 \
	-e AGENTBOX_CLI_BRIDGE_TIMEOUT=6 -e AGENTBOX_CLI_BRIDGE_CONCURRENCY=1 -e AGENTBOX_CLI_BRIDGE_QUEUE=1 \
	-e AGENTBOX_CLI_BRIDGE_MAX_OUTPUT_BYTES=100000 \
	--tmpfs /home/agent:uid=568,gid=568 --tmpfs /workspace:uid=568,gid=568 \
	"$image" >/dev/null
wait_healthy "$b" || bad "bridge container becomes healthy"
bport=$(docker port "$b" 3002/tcp | head -1 | sed 's/.*://')
api=http://127.0.0.1:$bport
check "the bridge answers /health" wait_for curl -fsS "$api/health"
docker exec "$b" touch /tmp/smoke-marker
state=/home/agent/.local/state/agentbox/cli-bridge

auth=(-H "Authorization: Bearer $token")
# chat MODEL CONTENT [EXTRA-JSON]: POST one user message; prints the status
# code, leaves the body in $work/body and the headers in $work/headers.
chat() {
	jq -cn --arg m "$1" --arg c "$2" --argjson x "${3:-"{}"}" '{model: $m, messages: [{role: "user", content: $c}]} + $x' |
		curl -s -o "$work/body" -D "$work/headers" -w '%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' \
			--data-binary @- "$api/v1/chat/completions"
}
# expect STATUS [JQ-FILTER] (after chat): status matches and the body passes the filter.
expect() { test "$status" = "$1" && jq -e "${2:-.}" "$work/body" >/dev/null; }
sse_text() { grep '^data: {' "$work/body" | sed 's/^data: //' | jq -rj '.choices[0].delta.content // empty'; }

check "/health without a token says only ok" bash -c "curl -fsS $api/health | jq -e '.status == \"ok\" and .adapters == null'"
check "/health with the token shows both adapters logged in" \
	bash -c "curl -fsS -H 'Authorization: Bearer $token' $api/health | jq -e '.adapters.claude.logged_in and .adapters.codex.logged_in'"
check "no token: 401" test "$(curl -s -o /dev/null -w '%{http_code}' "$api/v1/models")" = 401
check "wrong token: 401" test "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer x' "$api/v1/models")" = 401
check "no CORS headers, even for a preflight" \
	bash -c "! curl -s -i -X OPTIONS -H 'Origin: http://evil.example' $api/v1/chat/completions | grep -qi '^access-control-'"
check "/v1/models lists the enabled, logged-in adapters" bash -c "curl -fsS -H 'Authorization: Bearer $token' $api/v1/models |
	jq -e '[.data[].id] == [\"claude\",\"claude/sonnet\",\"claude/opus\",\"claude/haiku\",\"codex\",\"codex/gpt-stub-1\"]'"

for m in claude/sonnet codex; do
	status=$(chat "$m" "PROMPT-MARKER-$run_id say hi" '{"temperature": 0.3}')
	check "$m: answer, usage, finish_reason stop" \
		expect 200 '.choices[0].message.content == "Hello from the stub." and .choices[0].finish_reason == "stop" and .usage.prompt_tokens > 0'
	check "$m: temperature reported as ignored" grep -qi '^x-cli-bridge-ignored: temperature' "$work/headers"
	status=$(chat "$m" "say hi" '{"stream": true, "stream_options": {"include_usage": true}}')
	check "$m: streaming answer in SSE chunks" test "$status:$(sse_text)" = "200:Hello from the stub."
	check "$m: stream ends with finish_reason, usage chunk, [DONE]" bash -c "
		grep '^data: {' $work/body | tail -2 | head -1 | jq -e '.choices[0].finish_reason == \"stop\"' &&
		grep '^data: {' $work/body | tail -1 | jq -e '.choices == [] and .usage.total_tokens > 0' &&
		test \"\$(grep '^data:' $work/body | tail -1)\" = 'data: [DONE]'"
done
status=$(chat claude STUB:big)
check "output cap: answer cut at 100000 bytes, finish_reason length" \
	expect 200 '.choices[0].finish_reason == "length" and (.choices[0].message.content | utf8bytelength) == 100000'

status=$(chat claude STUB:limit)
check "claude usage limit: 429 with Retry-After" \
	bash -c "test $status = 429 && grep -qi '^retry-after: [0-9]' $work/headers && jq -e '.error.code == \"usage_limit_reached\"' $work/body"
status=$(chat codex STUB:limit)
check "codex usage limit: 429" expect 429 '.error.code == "usage_limit_reached"'
for m in claude codex; do
	status=$(chat "$m" STUB:tool)
	check "$m: a tool call stops the call (502 isolation_violation)" expect 502 '.error.code == "isolation_violation"'
done
status=$(chat claude STUB:crash)
check "a CLI failure is a 502 with its message" expect 502 '.error.message | test("unknown option")'
status=$(chat claude hi '{"tools": [{"type": "function", "function": {"name": "f"}}]}')
check "tools: 400" expect 400 '.error.param == "tools"'
status=$(chat claude hi '{"response_format": {"type": "json_schema", "json_schema": {}}}')
check "response_format json_schema: 400" expect 400 '.error.param == "response_format"'
status=$(chat claude hi '{"n": 2}')
check "n=2: 400" expect 400 '.error.param == "n"'
status=$(jq -cn '{model: "claude", messages: [{role: "user", content: [{type: "image_url", image_url: {url: "http://x/y.png"}}]}]}' |
	curl -s -o "$work/body" -w '%{http_code}' "${auth[@]}" --data-binary @- "$api/v1/chat/completions")
check "image content: 400" expect 400 '.error.message | test("images")'
status=$(chat gpt-4o hi)
check "unknown model: 404" expect 404 '.error.code == "model_not_found"'
head -c 2200000 /dev/zero | tr '\0' a | jq -Rsc '{model: "claude", messages: [{role: "user", content: .}]}' >"$work/large.json"
check "request over 2 MiB: 413" test "$(curl -s -o /dev/null -w '%{http_code}' "${auth[@]}" --data-binary @"$work/large.json" "$api/v1/chat/completions")" = 413

payload='STUB:echo $(touch /tmp/pwned) `touch /tmp/pwned2`; rm -rf ~ ; " '"'"' | cat && echo hi'
for m in claude codex; do
	status=$(chat "$m" "$payload")
	jq -r '.choices[0].message.content' "$work/body" >"$work/echo-$m.json" 2>/dev/null || true
	check "$m: shell metacharacters reach the CLI as literal text" \
		bash -c "test $status = 200 && jq -e --arg p \"\$1\" '.prompt == \$p' $work/echo-$m.json" _ "$payload"
	check "$m: environment carries no API key or token variable" \
		jq -e '[.env[] | select(test("^(ANTHROPIC_|OPENAI_|CODEX_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)"))] == []' "$work/echo-$m.json"
	check "$m: runs in the empty work directory with the bridge's HOME" \
		jq -e '.cwd == "/tmp/agentbox/cli-bridge/work" and .env_values.HOME == "/tmp/agentbox/cli-bridge/home"' "$work/echo-$m.json"
	check "$m: gets the default system prompt" jq -e '.system == "You are a helpful assistant."' "$work/echo-$m.json"
done
check "shell metacharacters were not executed" in_c_b '! ls /tmp/pwned /tmp/pwned2'
check "claude: own config dir, no tools, no MCP, no persistence, safe mode" jq -e --arg d "$state/claude" '
	.env_values.CLAUDE_CONFIG_DIR == $d and (.argv | index("--safe-mode")) and (.argv | index("--no-session-persistence"))
	and (.argv | index("--strict-mcp-config")) and (.argv | index("--disable-slash-commands"))
	and .argv[-4:] == ["--tools", "", "--disallowedTools", "*"]' "$work/echo-claude.json"
check "codex: own CODEX_HOME, ephemeral, no user config, read-only sandbox" jq -e --arg d "$state/codex" '
	.env_values.CODEX_HOME == $d and (.argv | index("--ephemeral")) and (.argv | index("--ignore-user-config"))
	and (.argv | index("read-only")) and (.argv | index("shell_tool")) and .argv[-1] == "-"' "$work/echo-codex.json"
messages=$(jq -cn '[{role: "system", content: "S1"}, {role: "user", content: "STUB:echo Q1"},
	{role: "assistant", content: "A1"}, {role: "developer", content: "S2"}, {role: "user", content: "Q2"}]')
jq -cn --argjson ms "$messages" '{model: "codex", messages: $ms}' |
	curl -s -o "$work/body" "${auth[@]}" --data-binary @- "$api/v1/chat/completions"
check "history becomes a transcript, system and developer join the system prompt" bash -c "
	jq -r '.choices[0].message.content' $work/body | jq -e '.system == \"S1\n\nS2\" and
	(.prompt | test(\"--- user [0-9a-f]+ ---\nSTUB:echo Q1\n\n--- assistant [0-9a-f]+ ---\nA1\n\n--- user [0-9a-f]+ ---\nQ2\n\$\"))'"

start=$(date +%s)
status=$(chat claude STUB:hang)
check "timeout: 504 after the configured 6 s" bash -c "test $status = 504 && test $(($(date +%s) - start)) -lt 15"
check "timeout: the CLI's whole process group is gone" in_c_b '! pgrep -f "sleep 987"'
curl -s -o /dev/null --max-time 2 "${auth[@]}" --data-binary '{"model":"codex","messages":[{"role":"user","content":"STUB:hang"}]}' \
	"$api/v1/chat/completions" || true
sleep 1
check "a client that hangs up stops the CLI" in_c_b '! pgrep -f "sleep 987"'
check "and is logged as 499" bash -c "docker logs $b 2>&1 | grep -q '499 POST /v1/chat/completions.*error=client_closed'"

slow='{"model":"claude","messages":[{"role":"user","content":"STUB:slow"}]}'
for i in 1 2; do
	curl -s -o "$work/slow$i" -w '%{http_code}' "${auth[@]}" --data-binary "$slow" "$api/v1/chat/completions" >"$work/slow$i.code" &
	sleep 0.5
done
status=$(chat claude hi)
check "queue full (1 running, 1 waiting): 429 queue_full" expect 429 '.error.code == "queue_full"'
wait
check "the running and the queued call both finish" test "$(cat "$work/slow1.code") $(cat "$work/slow2.code")" = "200 200"

status=$(chat claude STUB:write)
check "files a CLI leaves in the work directory are removed and logged" \
	bash -c "test $status = 200 && docker logs $b 2>&1 | grep -q 'left files in its working directory, removed: stray.txt'"
check "work and per-call directories are empty afterwards" \
	in_c_b 'test -z "$(ls -A /tmp/agentbox/cli-bridge/work /tmp/agentbox/cli-bridge/calls)"'
check "one log line per request with adapter, model, duration, tokens" \
	bash -c "docker logs $b 2>&1 | grep -q '200 POST /v1/chat/completions .*adapter=codex model=codex .*duration=.*prompt_tokens=11'"
check "logs carry neither prompts nor answers" \
	bash -c "! docker logs $b 2>&1 | grep -q -e 'PROMPT-MARKER-$run_id' -e 'Hello from the stub'"

docker exec "$b" touch "$state/claude/.stub-logged-out"
docker exec "$b" agentbox restart cli-bridge
sleep 2
check "after a restart the bridge answers again" wait_for curl -fsS "$api/health"
check "a logged-out adapter disappears from /v1/models" bash -c "curl -fsS -H 'Authorization: Bearer $token' $api/v1/models |
	jq -e '[.data[].id] == [\"codex\",\"codex/gpt-stub-1\"]'"
status=$(chat claude/sonnet hi)
check "a logged-out adapter: 401 not_logged_in" expect 401 '.error.code == "not_logged_in"'
docker exec "$b" rm "$state/claude/.stub-logged-out"
status=$(chat codex STUB:auth)
check "a login the CLI rejects mid-call: 401 not_logged_in" expect 401 '.error.code == "not_logged_in"'

echo "== cli-bridge with the real CLIs (no logins)"
status=$(chat claude 'STUB:real Say hi')
check "real claude takes every bridge flag; without a login: 401" expect 401 '.error.code == "not_logged_in"'
docker exec -d "$b" python3 -I /test/fake_responses.py 18080 /tmp/fake-responses.jsonl
sleep 1
status=$(chat codex 'STUB:real Say hi' '{"reasoning_effort": "low"}')
check "real codex takes every bridge flag and answers through the fake API" \
	expect 200 '.choices[0].message.content == "Hello from the fake Responses API." and .usage.prompt_tokens == 11'
docker exec "$b" cat /tmp/fake-responses.jsonl >"$work/fake.jsonl" 2>/dev/null || true
check "codex sent only the bridge's system prompt and the prompt" jq -se '
	[.[] | select(.path | endswith("/responses")) | .body.input[] | select(.type == "message")
	 | {role, text: .content[0].text}] == [{role: "developer", text: "You are a helpful assistant."},
	 {role: "user", text: "STUB:real Say hi"}]' "$work/fake.jsonl"
check "codex offered no tool beyond apply_patch / its JavaScript cell" jq -se '
	def names: .[] | if .type == "namespace" then (.tools | names) else (.name // .type) end;
	[.[] | .body | ((.tools // []) | names), (.input[] | select(.type == "additional_tools") | .tools | names)]
	| . - ["apply_patch", "exec", "wait"] == []' "$work/fake.jsonl"
check "codex used the requested reasoning effort" jq -se '[.[] | .body.reasoning.effort] | index("low")' "$work/fake.jsonl"

# Everything any of the calls above wrote, stubs and real CLIs alike.
docker exec "$b" find /home/agent /workspace /tmp -xdev -newer /tmp/smoke-marker ! -type d >"$work/written" 2>/dev/null || true
grep -v -e "^$state/" -e '^/tmp/agentbox/' -e '^/home/agent/\.cloudcli/' -e '^/tmp/fake-responses\.jsonl$' "$work/written" >"$work/stray" || true
if [ -s "$work/stray" ]; then
	bad "no files written outside the bridge's own directories"
	sed 's/^/     /' "$work/stray"
else ok "no files written outside the bridge's own directories"; fi
check "the CLIs wrote their state into the bridge's directories" \
	bash -c "grep -q '^$state/claude/' $work/written && grep -q '^$state/codex/' $work/written"
check "no session transcripts from the real CLIs" \
	in_c_b "! find $state -name '*.jsonl' | grep -q . && ! find $state/codex -path '*/sessions/*' | grep -q ."

echo "== cli-bridge refuses to start without a token"
docker run -d --name "$c-notoken" "${hardening[@]}" -e AGENTBOX_CLI_BRIDGE=1 -e AGENTBOX_CLI_BRIDGE_CLAUDE=1 \
	--tmpfs /home/agent:uid=568,gid=568 --tmpfs /workspace:uid=568,gid=568 "$image" >/dev/null
wait_healthy "$c-notoken" || bad "no-token container becomes healthy (CloudCLI unaffected)"
check "it says why" wait_for bash -c "docker logs $c-notoken 2>&1 | grep -q 'refusing to start: no token'"
sleep 3
check "and stays down instead of restarting" bash -c "
	docker exec $c-notoken s6-svstat -o up,wantedup /tmp/agentbox/service/cli-bridge | grep -qx 'false false' &&
	test \$(docker logs $c-notoken 2>&1 | grep -c 'refusing to start') = 1"
check "port 3002 stays closed" docker exec "$c-notoken" bash -c '! curl -s -o /dev/null --max-time 2 http://127.0.0.1:3002/health'

echo "== unit tests (the image's Python)"
if docker run --rm --entrypoint python3 -v "$repo/test:/test:ro" --tmpfs /tmp:exec "$image" \
	-I -m unittest discover -s /test/unit >"$work/unit.log" 2>&1; then
	ok "unit tests pass ($(grep -Eo 'Ran [0-9]+ tests' "$work/unit.log"))"
else
	bad "unit tests pass"
	tail -40 "$work/unit.log"
fi

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
