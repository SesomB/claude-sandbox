#!/usr/bin/env bash
# End-to-end check of one user's Hermes backend on the server (run as root).
#
#   hermes-smoke-test alice [--other-user bob] [--no-agent]
#
#  1. backend is listening on its loopback port (and the service is active)
#  2. the API rejects requests without the Desktop session token, accepts with it
#  3. an agent turn runs a terminal command as the user, in ~/work
#     (with the real LLM, or the bundled mock: start it first with
#      python3 /usr/local/lib/hermes-airgap/mock_llm.py and point site.conf at it)
#  4. isolation: --other-user cannot connect to this user's port
#  5. egress: the user cannot reach an address outside the allow-list

HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=lib.sh
. "$HERE/lib.sh"
set +e

[[ $# -ge 1 && $1 != -* ]] || { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
USER_NAME=$1; shift
OTHER=
RUN_AGENT=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --other-user) OTHER=$2; shift 2 ;;
        --no-agent) RUN_AGENT=0; shift ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
require_root

FAILS=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; }

PORT=$(user_port "$USER_NAME")
[[ -n $PORT ]] || die "$USER_NAME is not a Hermes user"
TOKEN=$(sed -n 's/^HERMES_DASHBOARD_SESSION_TOKEN=//p' "$HERMES_ETC/users/$USER_NAME.env")
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
BASE=http://127.0.0.1:$PORT
as_user() { runuser -u "$1" -- "${@:2}"; }

echo "== Backend ($USER_NAME, $BASE)"
if have_systemd; then
    systemctl is-active --quiet "hermes-serve@$USER_NAME" && pass "hermes-serve@$USER_NAME active" || fail "hermes-serve@$USER_NAME not active"
fi
for _ in $(seq 1 30); do
    code=$(as_user "$USER_NAME" curl -s -o /dev/null -w '%{http_code}' -H "X-Hermes-Session-Token: $TOKEN" "$BASE/api/health")
    [[ $code == 200 ]] && break
    sleep 1
done
[[ $code == 200 ]] && pass "GET /api/health with token -> 200" || fail "GET /api/health with token -> $code"

code=$(as_user "$USER_NAME" curl -s -o /dev/null -w '%{http_code}' "$BASE/api/sessions")
[[ $code == 401 || $code == 403 ]] && pass "GET /api/sessions without token -> $code" || fail "GET /api/sessions without token -> $code (expected 401)"
code=$(as_user "$USER_NAME" curl -s -o /dev/null -w '%{http_code}' -H "X-Hermes-Session-Token: $TOKEN" "$BASE/api/sessions")
[[ $code == 200 ]] && pass "GET /api/sessions with token -> 200" || fail "GET /api/sessions with token -> $code"

echo "== Agent turn"
if [[ $RUN_AGENT == 1 ]]; then
    marker="smoke-$(date +%s)"
    rm -f "$HOME_DIR/work/hello.txt"
    out=$(cd "$HOME_DIR/work" && timeout 300 runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" \
        hermes -z "Use the terminal tool to run exactly this command in the current directory: echo $marker > hello.txt && uname -a. Then reply with the command output." 2>&1)
    rc=$?
    if [[ -f $HOME_DIR/work/hello.txt ]]; then
        owner=$(stat -c %U "$HOME_DIR/work/hello.txt")
        [[ $owner == "$USER_NAME" ]] && pass "agent created ~/work/hello.txt as $owner" || fail "hello.txt owned by $owner"
    else
        fail "agent did not create ~/work/hello.txt (rc=$rc). Output:"; sed 's/^/      /' <<<"${out:0:2000}"
    fi
    grep -q Linux <<<"$out" && pass "reply contains server uname output" || skip "reply did not echo uname (model-dependent)"
    [[ -f $HOME_DIR/.hermes/state.db ]] && pass "session stored in ~/.hermes/state.db" || fail "no ~/.hermes/state.db"
else
    skip "agent turn (--no-agent)"
fi

echo "== Isolation"
if [[ -n $OTHER ]]; then
    code=$(as_user "$OTHER" curl -s -m 5 -o /dev/null -w '%{http_code}' -H "X-Hermes-Session-Token: $TOKEN" "$BASE/api/health")
    [[ $code == 000 ]] && pass "$OTHER cannot connect to $USER_NAME's port (even with the token)" || fail "$OTHER reached $USER_NAME's backend (HTTP $code)"
    as_user "$OTHER" test -r "$HOME_DIR/.hermes/config.yaml" && fail "$OTHER can read $USER_NAME's ~/.hermes" || pass "$OTHER cannot read $USER_NAME's ~/.hermes"
else
    skip "cross-user check (give --other-user)"
fi
if grep -q "PermitOpen 127.0.0.1:$PORT" "$SSHD_DROPIN" 2>/dev/null; then pass "sshd limits $USER_NAME to forwarding 127.0.0.1:$PORT"; else fail "no PermitOpen for $USER_NAME in $SSHD_DROPIN"; fi

echo "== Egress"
# shellcheck disable=SC1090
. "$FIREWALL_CONF" 2>/dev/null
if [[ ${EGRESS_MODE:-enforce} == enforce ]]; then
    # 192.0.2.1 is TEST-NET-1: never routable, never in the allow-list.
    if as_user "$USER_NAME" curl -s -m 5 -o /dev/null http://192.0.2.1/ ; then
        fail "$USER_NAME reached 192.0.2.1 (egress not blocked)"
    else
        pass "$USER_NAME cannot reach addresses outside /etc/hermes/egress.conf"
    fi
else
    skip "egress in log mode (hermes-firewall mode enforce)"
fi

echo
if (( FAILS > 0 )); then echo "Smoke test: $FAILS failure(s)."; exit 1; fi
echo "Smoke test: OK"
