# shellcheck shell=bash
# Shared helpers for the hermes-airgap server scripts.
# Sourced by install-server.sh, add-user.sh, remove-user.sh, firewall.sh,
# smoke-test.sh. Installed to /usr/local/lib/hermes-airgap/ on the server.

set -euo pipefail

HERMES_PREFIX=${HERMES_PREFIX:-/opt/hermes}
HERMES_ETC=${HERMES_ETC:-/etc/hermes}
HERMES_LIB=${HERMES_LIB:-/usr/local/lib/hermes-airgap}
HERMES_GROUP=${HERMES_GROUP:-hermes-users}
HERMES_PORT_MIN=${HERMES_PORT_MIN:-9121}
HERMES_PORT_MAX=${HERMES_PORT_MAX:-9199}
USERS_CONF="$HERMES_ETC/users.conf"      # "<user> <port>" per line
EGRESS_CONF="$HERMES_ETC/egress.conf"    # one IP or CIDR per line
FIREWALL_CONF="$HERMES_ETC/firewall.conf" # EGRESS_MODE=enforce|log
SSHD_DROPIN=${SSHD_DROPIN:-/etc/ssh/sshd_config.d/50-hermes.conf}
NFT_RENDERED="$HERMES_ETC/hermes.nft"

log()  { printf '\033[1;34m[hermes-airgap]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[hermes-airgap] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[hermes-airgap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ $(id -u) -eq 0 ]] || die "run this as root"
}

have_systemd() {
    [[ -d /run/systemd/system ]]
}

# Directory holding templates: next to the scripts in a checkout/bundle,
# or under $HERMES_LIB once installed.
templates_dir() {
    local here
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    for d in "$here/../../configs/hermes-airgap" "$here/../configs" "$here/templates" "$HERMES_LIB/templates"; do
        [[ -f "$d/config.yaml.tmpl" ]] && { (cd "$d" && pwd); return; }
    done
    die "templates not found (looked next to $here and in $HERMES_LIB/templates)"
}

# render_template SRC DEST KEY=VALUE...  — replaces @@KEY@@ with VALUE.
# Values may contain any character except newline.
render_template() {
    local src=$1 dest=$2; shift 2
    python3 - "$src" "$dest" "$@" <<'PY'
import sys
src, dest, *pairs = sys.argv[1:]
text = open(src).read()
for pair in pairs:
    key, _, value = pair.partition("=")
    text = text.replace("@@" + key + "@@", value)
if "@@" in text:
    import re
    left = sorted(set(re.findall(r"@@[A-Z0-9_]+@@", text)))
    if left:
        sys.exit("unrendered placeholders in %s: %s" % (src, ", ".join(left)))
open(dest, "w").write(text)
PY
}

random_token() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

# --- users.conf -----------------------------------------------------------

users_list() {
    [[ -f $USERS_CONF ]] || return 0
    grep -Ev '^\s*(#|$)' "$USERS_CONF" || true
}

user_port() {
    users_list | awk -v u="$1" '$1 == u { print $2 }'
}

next_free_port() {
    local p used
    used=$(users_list | awk '{ print $2 }')
    for ((p = HERMES_PORT_MIN; p <= HERMES_PORT_MAX; p++)); do
        grep -qx "$p" <<<"$used" && continue
        # Also skip anything already listening on that port.
        if command -v ss >/dev/null && ss -Hltn "sport = :$p" | grep -q .; then
            continue
        fi
        echo "$p"; return
    done
    die "no free port in $HERMES_PORT_MIN-$HERMES_PORT_MAX"
}

# --- sshd -----------------------------------------------------------------

render_sshd_dropin() {
    local tmp user port tunnel_only
    tmp=$(mktemp)
    {
        echo "# Managed by hermes-airgap (add-user.sh / remove-user.sh). Do not edit."
        echo "# Each Hermes user may only forward to their own backend port."
        while read -r user port tunnel_only _; do
            echo ""
            echo "Match User $user"
            echo "    AllowTcpForwarding local"
            echo "    PermitOpen 127.0.0.1:$port"
            echo "    PermitListen none"
            echo "    GatewayPorts no"
            echo "    X11Forwarding no"
            echo "    AllowAgentForwarding no"
            echo "    AllowStreamLocalForwarding no"
            echo "    PermitTunnel no"
            if [[ ${tunnel_only:-} == tunnel-only ]]; then
                echo "    PermitTTY no"
                echo "    ForceCommand /bin/echo 'This account only allows the Hermes Desktop SSH tunnel (ssh -N).'"
            fi
        done < <(users_list)
        echo ""
        echo "Match all"
    } >"$tmp"
    install -m 0644 "$tmp" "$SSHD_DROPIN"
    rm -f "$tmp"
    if command -v sshd >/dev/null; then
        sshd -t || die "sshd config test failed; check $SSHD_DROPIN"
        if have_systemd; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || warn "could not reload sshd"
        fi
    else
        warn "sshd not installed; wrote $SSHD_DROPIN anyway"
    fi
}

# --- nftables -------------------------------------------------------------

render_nft() {
    local tdir mode action port_elems="" uid_elems="" v4="" v6="" user port addr uid
    tdir=$(templates_dir)
    mode=enforce
    # shellcheck disable=SC1090
    [[ -f $FIREWALL_CONF ]] && . "$FIREWALL_CONF" && mode=${EGRESS_MODE:-enforce}
    case $mode in
        enforce) action="reject" ;;
        log)     action="accept" ;;
        *) die "EGRESS_MODE must be enforce or log (got '$mode')" ;;
    esac

    while read -r user port _; do
        uid=$(id -u "$user" 2>/dev/null) || { warn "user $user in $USERS_CONF does not exist; skipped"; continue; }
        port_elems+="${port_elems:+, }$port . $uid"
        uid_elems+="${uid_elems:+, }$uid"
    done < <(users_list)

    if [[ -f $EGRESS_CONF ]]; then
        while read -r addr _; do
            [[ -z $addr || $addr == \#* ]] && continue
            if [[ $addr == *:* ]]; then v6+="${v6:+, }$addr"; else v4+="${v4:+, }$addr"; fi
        done <"$EGRESS_CONF"
    fi

    elems() { [[ -n $1 ]] && echo "elements = { $1 }" || echo "# (empty)"; }
    render_template "$tdir/nftables-hermes.nft.tmpl" "$NFT_RENDERED.new" \
        "PORT_OWNER_ELEMENTS=$(elems "$port_elems")" \
        "HERMES_UID_ELEMENTS=$(elems "$uid_elems")" \
        "EGRESS_V4_ELEMENTS=$(elems "$v4")" \
        "EGRESS_V6_ELEMENTS=$(elems "$v6")" \
        "PORT_RANGE=$HERMES_PORT_MIN-$HERMES_PORT_MAX" \
        "EGRESS_ACTION=$action"
    nft -c -f "$NFT_RENDERED.new" || die "nftables rules failed validation ($NFT_RENDERED.new)"
    mv "$NFT_RENDERED.new" "$NFT_RENDERED"
}

apply_nft() {
    render_nft
    nft -f "$NFT_RENDERED"
    log "firewall loaded ($NFT_RENDERED)"
}
