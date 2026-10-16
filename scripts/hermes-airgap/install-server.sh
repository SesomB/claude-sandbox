#!/usr/bin/env bash
# Install (or upgrade to) a Hermes release on the air-gapped Debian 13 server.
#
#   install-server.sh --bundle hermes-airgap-server-v0.22.2.tar.gz \
#       --pypi-index https://pypi.internal/simple \
#       --llm-base-url http://llm.internal:8000/v1 --llm-model Hermes-4-70B \
#       --context-length 131072
#
# Layout it creates:
#   /opt/hermes/<ref>/{hermes-agent,python,venv,tools/bin}   one dir per release
#   /opt/hermes/current -> <ref>                             active release
#   /usr/local/bin/hermes                                     launcher
#   /usr/local/lib/hermes-airgap/                             these scripts + templates
#   /usr/local/sbin/hermes-{add-user,remove-user,firewall,smoke-test}
#   /etc/hermes/{site.conf,users.conf,egress.conf,firewall.conf,users/}
#   /etc/systemd/system/hermes-serve@.service, hermes-firewall.service
#
# Re-running with a newer bundle installs it side by side, backs up every
# user's state, flips /opt/hermes/current and restarts the backends.
# Roll back with:  install-server.sh --activate <old-ref>

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$HERE/lib.sh"

BUNDLE=
PYPI_INDEX=
USE_WHEELHOUSE=0
LLM_BASE_URL=
LLM_MODEL=
CONTEXT_LENGTH=
EGRESS_EXTRA=
EGRESS_MODE=
SKIP_APT=0
FORCE=0
ACTIVATE=
NO_RESTART=0

usage() {
    cat <<EOF
Usage: $0 --bundle FILE|DIR (--pypi-index URL | --wheelhouse) [options]
       $0 --activate REF          switch /opt/hermes/current to an installed release

Required on first install:
  --llm-base-url URL     internal OpenAI-compatible endpoint, ending in /v1
  --llm-model NAME       model id as served by that endpoint (GET /v1/models)
  --context-length N     context window the server serves (>= 65536)

Options:
  --pypi-index URL       internal PyPI mirror (simple index), e.g. https://nexus/repository/pypi/simple
  --wheelhouse           install from the wheelhouse shipped in the bundle instead
  --egress LIST          extra comma-separated IPs/CIDRs Hermes users may reach
  --egress-mode MODE     enforce (default) or log
  --skip-apt             don't apt-get install prerequisites
  --no-restart           install/flip but don't restart running backends
  --force                reinstall over an existing release; ignore OS checks
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --bundle) BUNDLE=$2; shift 2 ;;
        --pypi-index) PYPI_INDEX=$2; shift 2 ;;
        --wheelhouse) USE_WHEELHOUSE=1; shift ;;
        --llm-base-url) LLM_BASE_URL=${2%/}; shift 2 ;;
        --llm-model) LLM_MODEL=$2; shift 2 ;;
        --context-length) CONTEXT_LENGTH=$2; shift 2 ;;
        --egress) EGRESS_EXTRA=$2; shift 2 ;;
        --egress-mode) EGRESS_MODE=$2; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --no-restart) NO_RESTART=1; shift ;;
        --force) FORCE=1; shift ;;
        --activate) ACTIVATE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

require_root

restart_backends() {
    [[ $NO_RESTART == 1 ]] && { log "not restarting backends (--no-restart)"; return; }
    have_systemd || return 0
    local user port
    while read -r user port _; do
        log "restarting hermes-serve@$user"
        systemctl restart "hermes-serve@$user" || warn "hermes-serve@$user failed to restart; see journalctl -u hermes-serve@$user"
    done < <(users_list)
}

backup_user_state() {
    local stamp dest user home
    stamp=$(date +%Y%m%d-%H%M%S)
    while read -r user _; do
        home=$(getent passwd "$user" | cut -d: -f6) || continue
        [[ -d $home/.hermes ]] || continue
        dest=/var/backups/hermes/$stamp/$user
        mkdir -p "$dest"
        have_systemd && systemctl stop "hermes-serve@$user" 2>/dev/null || true
        # state.db is SQLite in WAL mode: copy db + wal + shm while stopped.
        cp -a "$home/.hermes/"state.db* "$home/.hermes/config.yaml" "$dest/" 2>/dev/null || true
        [[ -d $home/.hermes/memories ]] && cp -a "$home/.hermes/memories" "$dest/"
        chmod -R go-rwx "$dest"
    done < <(users_list)
    [[ -d /var/backups/hermes/$stamp ]] && log "user state backed up to /var/backups/hermes/$stamp"
    return 0
}

activate() {
    local ref=$1
    [[ -x $HERMES_PREFIX/$ref/venv/bin/python ]] || die "$HERMES_PREFIX/$ref is not an installed release"
    local old
    old=$(readlink "$HERMES_PREFIX/current" 2>/dev/null || true)
    if [[ $old == "$ref" ]]; then
        log "$ref is already active"
    else
        backup_user_state
        ln -sfn "$ref" "$HERMES_PREFIX/current.new"
        mv -T "$HERMES_PREFIX/current.new" "$HERMES_PREFIX/current"
        log "activated $ref (was: ${old:-none})"
    fi
    restart_backends
}

if [[ -n $ACTIVATE ]]; then
    activate "$ACTIVATE"
    exit 0
fi

[[ -n $BUNDLE ]] || { usage >&2; exit 2; }
[[ -n $PYPI_INDEX || $USE_WHEELHOUSE == 1 ]] || die "give --pypi-index URL or --wheelhouse"

# --- OS checks -------------------------------------------------------------
. /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 13 || $(uname -m) != x86_64 ]]; then
    [[ $FORCE == 1 ]] || die "expected Debian 13 amd64, found ${PRETTY_NAME:-unknown} $(uname -m) (use --force to override)"
    warn "not Debian 13 amd64; continuing because of --force"
fi

# --- site config (first install needs the LLM settings) ---------------------
mkdir -p "$HERMES_ETC/users"
chmod 0755 "$HERMES_ETC"; chmod 0700 "$HERMES_ETC/users"
if [[ -f $HERMES_ETC/site.conf ]]; then
    # shellcheck disable=SC1091
    . "$HERMES_ETC/site.conf"
fi
LLM_BASE_URL=${LLM_BASE_URL:-${SITE_LLM_BASE_URL:-}}
LLM_MODEL=${LLM_MODEL:-${SITE_LLM_MODEL:-}}
CONTEXT_LENGTH=${CONTEXT_LENGTH:-${SITE_CONTEXT_LENGTH:-}}
[[ -n $LLM_BASE_URL && -n $LLM_MODEL && -n $CONTEXT_LENGTH ]] \
    || die "first install needs --llm-base-url, --llm-model and --context-length"
[[ $CONTEXT_LENGTH =~ ^[0-9]+$ ]] && (( CONTEXT_LENGTH >= 65536 )) \
    || die "--context-length must be a number >= 65536 (Hermes refuses smaller windows for tool use)"

# --- prerequisites from the internal Debian mirror -------------------------
if [[ $SKIP_APT == 0 ]]; then
    log "installing prerequisites from the Debian mirror"
    DEBIAN_FRONTEND=noninteractive apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        ca-certificates curl python3 tar gzip openssh-server nftables iproute2 procps sqlite3
fi
for cmd in python3 tar curl nft; do command -v "$cmd" >/dev/null || die "missing $cmd"; done

# --- unpack the bundle -----------------------------------------------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
if [[ -d $BUNDLE ]]; then
    SRC=$(cd "$BUNDLE" && pwd)
else
    sums="$(dirname "$BUNDLE")/SHA256SUMS"
    if [[ -f $sums ]]; then
        log "verifying $(basename "$BUNDLE") against SHA256SUMS"
        (cd "$(dirname "$BUNDLE")" && grep -F " $(basename "$BUNDLE")" SHA256SUMS | sha256sum -c --quiet -) \
            || die "checksum mismatch for $BUNDLE"
    else
        warn "no SHA256SUMS next to $BUNDLE; skipping checksum verification"
    fi
    tar -xzf "$BUNDLE" -C "$WORK"
    SRC=$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n1)
fi
[[ -f $SRC/MANIFEST.json && -d $SRC/hermes-agent ]] || die "$BUNDLE is not a hermes-airgap server bundle"
REF=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hermes_agent"]["ref"])' "$SRC/MANIFEST.json")
DEST=$HERMES_PREFIX/$REF
log "installing Hermes $REF into $DEST"

if [[ -e $DEST ]]; then
    [[ $FORCE == 1 ]] || die "$DEST already exists (use --force to reinstall, or --activate $REF)"
    [[ $(readlink "$HERMES_PREFIX/current" 2>/dev/null) == "$REF" ]] && die "refusing to reinstall the ACTIVE release; activate another one first"
    rm -rf "$DEST"
fi
mkdir -p "$HERMES_PREFIX"
# Build in the final location (venvs embed absolute paths); remove it again
# if anything below fails, so a half-built release is never left behind.
STAGING=$DEST
INSTALL_OK=0
trap 'rm -rf "$WORK"; [[ $INSTALL_OK == 1 ]] || rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING/tools/bin" "$STAGING/python"

cp -a "$SRC/hermes-agent" "$STAGING/hermes-agent"
cp -a "$SRC/MANIFEST.json" "$SRC/requirements.txt" "$STAGING/"

art() { awk -F'\t' -v n="$1" '$1==n{print $3}' "$SRC/artifacts/artifacts.tsv" | xargs basename; }
tar -xzf "$SRC/artifacts/$(art python)" -C "$STAGING/python" --strip-components=1
tar -xzf "$SRC/artifacts/$(art uv)" -C "$STAGING/tools/bin" --strip-components=1 --wildcards '*/uv' '*/uvx'
tar -xzf "$SRC/artifacts/$(art ripgrep)" -C "$STAGING/tools/bin" --strip-components=1 --wildcards '*/rg'

# --- Python 3.14 venv + hashed dependencies --------------------------------
log "creating venv (CPython $("$STAGING/python/bin/python3" -c 'import platform; print(platform.python_version())'))"
"$STAGING/python/bin/python3" -m venv "$STAGING/venv"
UV_ARGS=(pip install -q --python "$STAGING/venv/bin/python" --require-hashes --no-deps
         --compile-bytecode -r "$STAGING/requirements.txt")
if [[ $USE_WHEELHOUSE == 1 ]]; then
    [[ -d $SRC/wheelhouse ]] || die "bundle has no wheelhouse (build it with --with-wheelhouse)"
    UV_ARGS+=(--no-index --find-links "$SRC/wheelhouse")
    log "installing $(grep -cE '^[A-Za-z0-9]' "$STAGING/requirements.txt") packages from the bundled wheelhouse"
else
    UV_ARGS+=(--index-url "$PYPI_INDEX")
    log "installing $(grep -cE '^[A-Za-z0-9]' "$STAGING/requirements.txt") packages from $PYPI_INDEX"
fi
UV_NO_CONFIG=1 UV_CACHE_DIR="$WORK/uv-cache" UV_PYTHON_DOWNLOADS=never \
    "$STAGING/tools/bin/uv" "${UV_ARGS[@]}" \
    || die "dependency install failed. If packages are missing from the mirror, rebuild the bundle with --with-wheelhouse and use --wheelhouse"

"$STAGING/venv/bin/python" -m compileall -q -j 0 "$STAGING/hermes-agent" >/dev/null 2>&1 || true

# Sanity check before activation.
if ! HERMES_HOME="$WORK/home" HOME="$WORK" HERMES_ROOT="$STAGING" HERMES_DISABLE_LAZY_INSTALLS=1 \
        PATH="$STAGING/tools/bin:$PATH" timeout 120 \
        "$STAGING/venv/bin/python" "$STAGING/hermes-agent/hermes" --version >"$WORK/version.txt" 2>&1; then
    cat "$WORK/version.txt" >&2
    die "hermes --version failed in the new release; nothing was activated"
fi
log "$(head -n1 "$WORK/version.txt")"

chown -R root:root "$DEST"
chmod -R go-w "$DEST"
INSTALL_OK=1

# --- tooling, launcher, units ---------------------------------------------
log "installing tooling to $HERMES_LIB"
rm -rf "$HERMES_LIB.new"; mkdir -p "$HERMES_LIB.new/templates"
cp -a "$SRC/airgap/scripts/"*.sh "$HERMES_LIB.new/"
cp -a "$SRC/airgap/configs/"* "$HERMES_LIB.new/templates/"
cp -a "$SRC/airgap/tests/mock_llm.py" "$HERMES_LIB.new/"
[[ -f $SRC/airgap/docs/README.md ]] && cp -a "$SRC/airgap/docs/"*.md "$HERMES_LIB.new/"
chmod 0755 "$HERMES_LIB.new/"*.sh
rm -rf "$HERMES_LIB"; mv -T "$HERMES_LIB.new" "$HERMES_LIB"
for s in add-user remove-user firewall smoke-test preflight; do
    ln -sfn "$HERMES_LIB/$s.sh" "/usr/local/sbin/hermes-$s"
done
install -m 0755 "$HERMES_LIB/templates/hermes-wrapper.sh" /usr/local/bin/hermes

cat >"$HERMES_ETC/site.conf" <<EOF
# Defaults for hermes-add-user (written by install-server.sh)
SITE_LLM_BASE_URL=$LLM_BASE_URL
SITE_LLM_MODEL=$LLM_MODEL
SITE_CONTEXT_LENGTH=$CONTEXT_LENGTH
EOF
touch "$USERS_CONF"
getent group "$HERMES_GROUP" >/dev/null || groupadd --system "$HERMES_GROUP"

# Egress allow-list: LLM endpoint + DNS resolvers + --egress.
if [[ ! -f $EGRESS_CONF || -n $EGRESS_EXTRA ]]; then
    llm_host=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "$LLM_BASE_URL")
    {
        echo "# Destinations Hermes users may reach (one IP/CIDR per line)."
        echo "# Apply changes with: hermes-firewall reload"
        echo "# LLM endpoint: $LLM_BASE_URL"
        getent ahosts "$llm_host" | awk '{print $1}' | sort -u
        echo "# DNS resolvers (/etc/resolv.conf)"
        awk '/^nameserver/{print $2}' /etc/resolv.conf | grep -v '^127\.' || true
        if [[ -n $EGRESS_EXTRA ]]; then echo "# --egress"; tr ',' '\n' <<<"$EGRESS_EXTRA"; fi
        [[ -f $EGRESS_CONF ]] && grep -Ev '^\s*(#|$)' "$EGRESS_CONF" || true
    } | awk '!seen[$0]++' >"$EGRESS_CONF.new"
    mv "$EGRESS_CONF.new" "$EGRESS_CONF"
fi
if [[ -n $EGRESS_MODE || ! -f $FIREWALL_CONF ]]; then
    echo "EGRESS_MODE=${EGRESS_MODE:-enforce}" >"$FIREWALL_CONF"
fi

install -m 0644 "$HERMES_LIB/templates/hermes-serve@.service" /etc/systemd/system/
install -m 0644 "$HERMES_LIB/templates/hermes-firewall.service" /etc/systemd/system/
render_sshd_dropin
render_nft
if have_systemd; then
    systemctl daemon-reload
    systemctl enable --now hermes-firewall.service
    systemctl restart hermes-firewall.service
else
    warn "systemd not running; loading firewall directly"
    nft -f "$NFT_RENDERED"
fi

activate "$REF"

cat >&2 <<EOF

[hermes-airgap] Hermes $REF installed and active.
Next steps:
  1. hermes-preflight                     # check LLM endpoint + context length
  2. hermes-add-user alice --pubkey /path/to/alice.pub
  3. hermes-smoke-test alice
Installed releases: $(ls -1 "$HERMES_PREFIX" | grep -v '^current$' | tr '\n' ' ')
EOF
