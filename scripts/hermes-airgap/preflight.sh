#!/usr/bin/env bash
# Pre-install / health checks on the air-gapped server. Read-only.
#
#   preflight.sh --llm-base-url http://llm.internal:8000/v1 --llm-model Hermes-4-70B \
#                [--llm-api-key KEY] [--pypi-index URL --requirements FILE]
#
# After install-server.sh the LLM settings default to /etc/hermes/site.conf:
#   hermes-preflight
#
# Exit code: 0 = all PASS (WARNs allowed), 1 = at least one FAIL.

set -uo pipefail

LLM_BASE_URL=
LLM_MODEL=
LLM_API_KEY=${HERMES_LLM_API_KEY:-}
PYPI_INDEX=
REQUIREMENTS=
MIN_CONTEXT=65536

[[ -f /etc/hermes/site.conf ]] && . /etc/hermes/site.conf
LLM_BASE_URL=${SITE_LLM_BASE_URL:-}
LLM_MODEL=${SITE_LLM_MODEL:-}
CONTEXT_LENGTH=${SITE_CONTEXT_LENGTH:-}
[[ -f /opt/hermes/current/requirements.txt ]] && REQUIREMENTS=/opt/hermes/current/requirements.txt

while [[ $# -gt 0 ]]; do
    case $1 in
        --llm-base-url) LLM_BASE_URL=${2%/}; shift 2 ;;
        --llm-model) LLM_MODEL=$2; shift 2 ;;
        --llm-api-key) LLM_API_KEY=$2; shift 2 ;;
        --context-length) CONTEXT_LENGTH=$2; shift 2 ;;
        --pypi-index) PYPI_INDEX=${2%/}; shift 2 ;;
        --requirements) REQUIREMENTS=$2; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done

FAILS=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }

echo "== Host"
. /etc/os-release 2>/dev/null
if [[ ${ID:-} == debian && ${VERSION_ID:-} == 13 ]]; then pass "OS: $PRETTY_NAME"; else fail "OS: ${PRETTY_NAME:-unknown} (expected Debian 13)"; fi
if [[ $(uname -m) == x86_64 ]]; then pass "arch: x86_64"; else fail "arch: $(uname -m) (bundle is amd64)"; fi
glibc=$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+$')
[[ -n $glibc ]] && pass "glibc $glibc" || warn "could not determine glibc version"
avail=$(df -Pm /opt 2>/dev/null | awk 'NR==2{print $4}')
if [[ -n $avail ]] && (( avail >= 3000 )); then pass "free space on /opt: ${avail} MB"; else warn "free space on /opt: ${avail:-?} MB (want >= 3000 per release)"; fi
for cmd in python3 curl tar nft sshd; do
    p=$(command -v "$cmd" || ls /usr/sbin/"$cmd" 2>/dev/null)
    [[ -n $p ]] && pass "found $cmd" || warn "missing $cmd (install-server.sh installs it from the Debian mirror)"
done
if command -v sshd >/dev/null || [[ -x /usr/sbin/sshd ]]; then
    if grep -qsE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        pass "sshd_config includes sshd_config.d/*.conf"
    else
        fail "/etc/ssh/sshd_config does not Include /etc/ssh/sshd_config.d/*.conf (needed for per-user PermitOpen)"
    fi
fi

echo "== LLM endpoint"
if [[ -z $LLM_BASE_URL ]]; then
    fail "no --llm-base-url (and no /etc/hermes/site.conf)"
else
    auth=()
    [[ -n $LLM_API_KEY ]] && auth=(-H "Authorization: Bearer $LLM_API_KEY")
    body=$(curl -sS -m 15 "${auth[@]}" "$LLM_BASE_URL/models" 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        fail "GET $LLM_BASE_URL/models: $body"
    else
        pass "GET $LLM_BASE_URL/models answered"
        python3 - "$body" "${LLM_MODEL:-}" "${CONTEXT_LENGTH:-}" "$MIN_CONTEXT" <<'PY'
import json, sys
body, model, configured, minimum = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
P, W, F = "  \033[32mPASS\033[0m", "  \033[33mWARN\033[0m", "  \033[31mFAIL\033[0m"
try:
    data = json.loads(body).get("data", [])
except Exception:
    print(F, "response is not OpenAI-style JSON:", body[:200]); sys.exit(1)
ids = [m.get("id") for m in data]
print(P if ids else W, "models served:", ", ".join(map(str, ids)) or "(none)")
if model:
    m = next((m for m in data if m.get("id") == model), None)
    if not m:
        print(F, f"model '{model}' is not served by this endpoint"); sys.exit(1)
    print(P, f"model '{model}' is served")
    ctx = next((m.get(k) for k in ("max_model_len", "context_length", "context_window", "max_context_length") if m.get(k)), None)
    if ctx is None:
        print(W, "endpoint does not report a context length; make sure it serves >=", minimum)
    elif int(ctx) < minimum:
        print(F, f"served context {ctx} < {minimum} (Hermes refuses tool use below 64K)"); sys.exit(1)
    else:
        print(P, f"served context {ctx}")
        if configured and int(configured) > int(ctx):
            print(F, f"configured context_length {configured} > served {ctx}"); sys.exit(1)
PY
        [[ $? -eq 0 ]] || FAILS=$((FAILS + 1))
    fi
    if [[ -n $LLM_MODEL ]]; then
        resp=$(curl -sS -m 60 "${auth[@]}" -H 'Content-Type: application/json' "$LLM_BASE_URL/chat/completions" -d @- 2>&1 <<EOF
{"model": "$LLM_MODEL", "max_tokens": 64, "messages": [{"role": "user", "content": "What is 2+2? Use the tool."}],
 "tools": [{"type": "function", "function": {"name": "add", "description": "Add two integers",
   "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}}]}
EOF
)
        if grep -q '"tool_calls"' <<<"$resp" && ! grep -q '"tool_calls": *\[\]' <<<"$resp"; then
            pass "model returned a structured tool call (server-side tool-call parsing works)"
        elif grep -q '"choices"' <<<"$resp"; then
            warn "chat works but no structured tool_calls came back — check the server's tool parser (vLLM: --enable-auto-tool-choice --tool-call-parser hermes)"
        else
            fail "POST /chat/completions: ${resp:0:300}"
        fi
    fi
fi

if [[ -n $PYPI_INDEX && -n $REQUIREMENTS ]]; then
    echo "== PyPI mirror ($PYPI_INDEX)"
    missing=0; total=0
    while read -r name version; do
        total=$((total + 1))
        norm=$(tr 'A-Z_.' 'a-z--' <<<"$name")
        if ! curl -fsS -m 20 "$PYPI_INDEX/$norm/" 2>/dev/null | grep -qiE "[-_]${version//./\\.}([-.]|\.tar)"; then
            fail "mirror lacks $name==$version"; missing=$((missing + 1))
        fi
    done < <(grep -oE '^[A-Za-z0-9._-]+==[^ ;\\]+' "$REQUIREMENTS" | tr '=' ' ' | awk '{print $1, $2}')
    (( missing == 0 )) && pass "all $total pinned packages found on the mirror"
fi

echo
if (( FAILS > 0 )); then echo "Preflight: $FAILS failure(s)."; exit 1; fi
echo "Preflight: OK"
