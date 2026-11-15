# shellcheck shell=bash
# Shared by init, the service scripts and the agentbox command.

AGENTBOX_RUNTIME=/tmp/agentbox
AGENTBOX_SCANDIR=$AGENTBOX_RUNTIME/service
export AGENTBOX_RUNTIME AGENTBOX_SCANDIR

log() {
	printf '[agentbox%s] %s\n' "${SVC:+/$SVC}" "$*" >&2
}

die() {
	log "error: $*"
	exit 1
}

# Without a passwd entry for the current UID, ssh refuses to run and Node's
# os.userInfo() throws. For a UID other than the one the image was built for,
# nss_wrapper supplies the entry. init writes the files; services inherit the
# variables, interactive shells get them from /etc/bash.bashrc.
nss_env_file=$AGENTBOX_RUNTIME/nss.env

nss_setup() {
	getent passwd "$(id -u)" >/dev/null && return 0
	local dir=$AGENTBOX_RUNTIME/nss lib
	lib=$(find /usr/lib -name libnss_wrapper.so -print -quit)
	[ -n "$lib" ] || die "UID $(id -u) has no passwd entry and libnss_wrapper is missing"
	mkdir -p "$dir"
	{ cat /etc/passwd; printf 'agent:x:%s:%s:agent:%s:/bin/bash\n' "$(id -u)" "$(id -g)" "$HOME"; } >"$dir/passwd"
	cat /etc/group >"$dir/group"
	getent group "$(id -g)" >/dev/null || printf 'agent:x:%s:\n' "$(id -g)" >>"$dir/group"
	cat >"$nss_env_file" <<-EOF
		export LD_PRELOAD=$lib
		export NSS_WRAPPER_PASSWD=$dir/passwd
		export NSS_WRAPPER_GROUP=$dir/group
	EOF
	# shellcheck source=/dev/null
	. "$nss_env_file"
	log "UID $(id -u) is not in /etc/passwd; using nss_wrapper"
}

# wait_for_login NAME HINT CMD...: returns once CMD exits 0, polling every
# 30 s. Logs once instead of on every poll, so an unused agent stays quiet.
wait_for_login() {
	local name=$1 hint=$2
	shift 2
	"$@" >/dev/null 2>&1 && return 0
	log "$name is not logged in; idle until it is. Log in once: $hint"
	until "$@" >/dev/null 2>&1; do
		sleep 30 &
		wait $!
	done
	log "$name login found"
}
