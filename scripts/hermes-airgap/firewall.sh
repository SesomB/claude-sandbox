#!/usr/bin/env bash
# Manage the Hermes nftables table (backend-port isolation + egress allow-list).
#
#   hermes-firewall reload          re-render from users.conf/egress.conf and load
#   hermes-firewall status          show the loaded table with counters
#   hermes-firewall mode enforce    block egress outside the allow-list (default)
#   hermes-firewall mode log        only log it (commissioning / troubleshooting)
#   hermes-firewall denied          show recent denials from the kernel log

HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=lib.sh
. "$HERE/lib.sh"

require_root
case ${1:-} in
    reload) apply_nft ;;
    status) nft list table inet hermes ;;
    mode)
        [[ ${2:-} == enforce || ${2:-} == log ]] || die "usage: $0 mode enforce|log"
        echo "EGRESS_MODE=$2" >"$FIREWALL_CONF"
        apply_nft
        ;;
    denied)
        if command -v journalctl >/dev/null; then
            journalctl -k --since "${2:-1 hour ago}" --no-pager | grep -E 'hermes-(egress|port-deny)' || echo "no denials"
        else
            dmesg | grep -E 'hermes-(egress|port-deny)' || echo "no denials"
        fi
        ;;
    *) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
