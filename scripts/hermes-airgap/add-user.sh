#!/usr/bin/env bash
# Give a person their own Hermes backend on this server.
#
#   hermes-add-user alice --pubkey /tmp/alice.pub [--tunnel-only] [--ask-llm-key]
#
# Creates (or reuses) the Linux account, assigns a loopback port, writes
# ~/.hermes/config.yaml + .env, generates the Desktop session token,
# restricts sshd forwarding to that port, adds the per-port firewall rule
# and starts hermes-serve@<user>. Prints what the user needs on Windows.
#
# Re-running for an existing user keeps port, token and config unless
# --reconfigure (rewrite config.yaml from the site defaults) or
# --rotate-token is given.

HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=lib.sh
. "$HERE/lib.sh"

USER_NAME=
PORT=
PUBKEY=
TUNNEL_ONLY=0
ASK_KEY=0
RECONFIGURE=0
ROTATE=0
NO_START=0
LLM_MODEL_OVERRIDE=

usage() {
    cat <<EOF
Usage: $0 USER [options]
  --port N            backend port (default: next free in $HERMES_PORT_MIN-$HERMES_PORT_MAX)
  --pubkey FILE|KEY   SSH public key to authorise (file path or the key itself)
  --tunnel-only       account may only open the tunnel (no shell over SSH)
  --ask-llm-key       prompt for the LLM API key (stored in ~/.hermes/.env)
  --model NAME        per-user model (default: site model)
  --reconfigure       rewrite ~/.hermes/config.yaml from the template
  --rotate-token      generate a new Desktop session token
  --no-start          don't enable/start hermes-serve@USER
EOF
}

[[ $# -ge 1 && $1 != -* ]] || { usage >&2; exit 2; }
USER_NAME=$1; shift
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT=$2; shift 2 ;;
        --pubkey) PUBKEY=$2; shift 2 ;;
        --tunnel-only) TUNNEL_ONLY=1; shift ;;
        --ask-llm-key) ASK_KEY=1; shift ;;
        --model) LLM_MODEL_OVERRIDE=$2; shift 2 ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --rotate-token) ROTATE=1; shift ;;
        --no-start) NO_START=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

require_root
[[ $USER_NAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid user name '$USER_NAME'"
[[ -f $HERMES_ETC/site.conf ]] || die "run install-server.sh first"
# shellcheck disable=SC1091
. "$HERMES_ETC/site.conf"
TDIR=$(templates_dir)

# --- account ---------------------------------------------------------------
if id "$USER_NAME" >/dev/null 2>&1; then
    log "reusing existing account $USER_NAME"
    usermod -aG "$HERMES_GROUP" "$USER_NAME"
else
    log "creating account $USER_NAME"
    useradd --create-home --shell /bin/bash --groups "$HERMES_GROUP" "$USER_NAME"
    passwd -l "$USER_NAME" >/dev/null   # SSH keys only
fi
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
[[ $HOME_DIR == "/home/$USER_NAME" ]] \
    || warn "home is $HOME_DIR, but hermes-serve@.service assumes /home/$USER_NAME — add a drop-in override"
chmod 0750 "$HOME_DIR"
GROUP_NAME=$(id -gn "$USER_NAME")

as_user() { runuser -u "$USER_NAME" -- "$@"; }

# --- port --------------------------------------------------------------------
EXISTING_PORT=$(user_port "$USER_NAME")
if [[ -n $EXISTING_PORT ]]; then
    [[ -z $PORT || $PORT == "$EXISTING_PORT" ]] || die "$USER_NAME already has port $EXISTING_PORT (remove the user first to change it)"
    PORT=$EXISTING_PORT
else
    if [[ -n $PORT ]]; then
        (( PORT >= HERMES_PORT_MIN && PORT <= HERMES_PORT_MAX )) || die "port must be in $HERMES_PORT_MIN-$HERMES_PORT_MAX"
        users_list | awk '{print $2}' | grep -qx "$PORT" && die "port $PORT is taken"
    else
        PORT=$(next_free_port)
    fi
    echo "$USER_NAME $PORT$([[ $TUNNEL_ONLY == 1 ]] && echo ' tunnel-only')" >>"$USERS_CONF"
fi
if [[ $TUNNEL_ONLY == 1 ]] && ! users_list | awk -v u="$USER_NAME" '$1==u && $3=="tunnel-only"' | grep -q .; then
    sed -i -E "s/^($USER_NAME $PORT)\$/\1 tunnel-only/" "$USERS_CONF"
fi

# --- SSH key -------------------------------------------------------------
if [[ -n $PUBKEY ]]; then
    [[ -f $PUBKEY ]] && PUBKEY=$(cat "$PUBKEY")
    [[ $PUBKEY =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-) ]] || die "--pubkey does not look like an SSH public key"
    as_user mkdir -p "$HOME_DIR/.ssh"
    chmod 0700 "$HOME_DIR/.ssh"
    touch "$HOME_DIR/.ssh/authorized_keys"
    if ! grep -qxF "$PUBKEY" "$HOME_DIR/.ssh/authorized_keys"; then
        echo "$PUBKEY" >>"$HOME_DIR/.ssh/authorized_keys"
        log "authorised SSH key for $USER_NAME"
    fi
    chown "$USER_NAME:$GROUP_NAME" "$HOME_DIR/.ssh/authorized_keys"
    chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
fi

# --- token (root-only env file read by systemd) ---------------------------
UENV=$HERMES_ETC/users/$USER_NAME.env
TOKEN=
if [[ -f $UENV && $ROTATE == 0 ]]; then
    TOKEN=$(sed -n 's/^HERMES_DASHBOARD_SESSION_TOKEN=//p' "$UENV")
fi
[[ -n $TOKEN ]] || TOKEN=$(random_token)
umask 077
printf 'HERMES_PORT=%s\nHERMES_DASHBOARD_SESSION_TOKEN=%s\n' "$PORT" "$TOKEN" >"$UENV"
umask 022

# --- HERMES_HOME + workspace ---------------------------------------------
HH=$HOME_DIR/.hermes
WORKDIR=$HOME_DIR/work
as_user mkdir -p "$HH" "$WORKDIR"
chmod 0700 "$HH"
if [[ ! -f $HH/config.yaml || $RECONFIGURE == 1 ]]; then
    [[ -f $HH/config.yaml ]] && cp -a "$HH/config.yaml" "$HH/config.yaml.bak.$(date +%s)"
    render_template "$TDIR/config.yaml.tmpl" "$HH/config.yaml" \
        "LLM_BASE_URL=$SITE_LLM_BASE_URL" \
        "LLM_MODEL=${LLM_MODEL_OVERRIDE:-$SITE_LLM_MODEL}" \
        "CONTEXT_LENGTH=$SITE_CONTEXT_LENGTH" \
        "WORKDIR=$WORKDIR"
    chown "$USER_NAME:$GROUP_NAME" "$HH/config.yaml"; chmod 0600 "$HH/config.yaml"
    log "wrote $HH/config.yaml"
fi
LLM_KEY=
if [[ $ASK_KEY == 1 ]]; then
    read -r -s -p "LLM API key for $USER_NAME (empty for none): " LLM_KEY; echo >&2
fi
if [[ ! -f $HH/.env || $ASK_KEY == 1 ]]; then
    render_template "$TDIR/env.tmpl" "$HH/.env" "USER=$USER_NAME" "LLM_API_KEY=$LLM_KEY"
    chown "$USER_NAME:$GROUP_NAME" "$HH/.env"; chmod 0600 "$HH/.env"
fi
# Connection details the user can read back later (their own token only).
cat >"$HH/desktop-connection.txt" <<EOF
Hermes Desktop connection for $USER_NAME
  Server port (tunnel target): 127.0.0.1:$PORT
  Desktop remote URL:          http://127.0.0.1:9119   (through the SSH tunnel)
  Desktop remote token:        $TOKEN
EOF
chown "$USER_NAME:$GROUP_NAME" "$HH/desktop-connection.txt"; chmod 0600 "$HH/desktop-connection.txt"

# --- sshd + firewall + service --------------------------------------------
render_sshd_dropin
apply_nft
if [[ $NO_START == 0 ]]; then
    if have_systemd; then
        systemctl enable "hermes-serve@$USER_NAME" >/dev/null 2>&1
        systemctl restart "hermes-serve@$USER_NAME"
        sleep 2
        systemctl is-active --quiet "hermes-serve@$USER_NAME" \
            || die "hermes-serve@$USER_NAME is not running; see: journalctl -u hermes-serve@$USER_NAME -n 50"
        log "hermes-serve@$USER_NAME is running on 127.0.0.1:$PORT"
    else
        warn "systemd not running; start the backend yourself (see hermes-serve@.service)"
    fi
fi

SERVER_NAME=$(hostname -f 2>/dev/null || hostname)
cat <<EOF

================  Give this to $USER_NAME  ================
On the Windows workstation (PowerShell, in the folder with the scripts):

  .\\Start-HermesTunnel.ps1 -Server $SERVER_NAME -User $USER_NAME -RemotePort $PORT

Hermes Desktop -> Settings -> Gateways -> Remote gateway
  URL:   http://127.0.0.1:9119
  Token: $TOKEN

(Saved for the user in $HH/desktop-connection.txt)
============================================================
EOF
