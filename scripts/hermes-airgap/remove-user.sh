#!/usr/bin/env bash
# Stop and deregister a user's Hermes backend.
#
#   hermes-remove-user alice                 # keep account and ~/.hermes data
#   hermes-remove-user alice --delete-account   # also userdel -r (irreversible)

HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=lib.sh
. "$HERE/lib.sh"

[[ $# -ge 1 && $1 != -* ]] || { echo "Usage: $0 USER [--delete-account]" >&2; exit 2; }
USER_NAME=$1; shift
DELETE=0
[[ ${1:-} == --delete-account ]] && DELETE=1

require_root
[[ -n $(user_port "$USER_NAME") ]] || warn "$USER_NAME is not in $USERS_CONF"

if have_systemd; then
    systemctl disable --now "hermes-serve@$USER_NAME" 2>/dev/null || true
fi
sed -i -E "/^$USER_NAME[[:space:]]/d" "$USERS_CONF"
rm -f "$HERMES_ETC/users/$USER_NAME.env"
render_sshd_dropin
apply_nft
log "removed Hermes backend for $USER_NAME"

if [[ $DELETE == 1 ]]; then
    read -r -p "Delete account $USER_NAME and its home directory? Type the user name to confirm: " answer
    [[ $answer == "$USER_NAME" ]] || die "not confirmed; account kept"
    userdel -r "$USER_NAME"
    log "deleted account $USER_NAME"
else
    log "account and $(getent passwd "$USER_NAME" | cut -d: -f6)/.hermes kept"
fi
