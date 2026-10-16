#!/usr/bin/env bash
# Offline end-to-end test of the server side, inside a network-less Debian 13
# container (no systemd: backends are started by hand, the way the unit would).
#
# Host side:
#   docker build -t hermes-airgap-testbase - <<'EOF'
#   FROM debian:13
#   RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
#       python3 tar gzip openssh-server openssh-client nftables iproute2 procps sqlite3 && mkdir -p /run/sshd
#   EOF
#   docker run --rm --network none --cap-add NET_ADMIN \
#       -v "$PWD:/repo:ro" -v /path/to/bundle-out:/out:ro \
#       hermes-airgap-testbase /repo/tests/hermes-airgap/container-e2e.sh
#
# The base image stands in for "prerequisites from the internal Debian
# mirror"; the bundle's wheelhouse stands in for the internal PyPI mirror;
# mock_llm.py stands in for the internal LLM endpoint.

set -euo pipefail
REPO=${REPO:-/repo}
BUNDLE=${BUNDLE:-$(ls /out/hermes-airgap-server-*.tar.gz | head -n1)}
step() { printf '\n\033[1;35m=== %s ===\033[0m\n' "$*"; }

step "network check (must be offline)"
# --network none: no route at all. With a network attached (E2E_NETWORK=1)
# the container must still not reach the internet directly; that mode lets
# the nftables egress counters see every connection attempt (leak check).
if curl -s -m 5 -o /dev/null --noproxy '*' https://pypi.org; then echo "container reaches the internet; aborting"; exit 1; fi
echo "offline: OK"

step "mock LLM on 127.0.0.1:8000"
python3 "$REPO/tests/hermes-airgap/mock_llm.py" --port 8000 >/tmp/mock_llm.log 2>&1 &
sleep 1

step "install-server.sh"
# By default, test the scripts in $REPO rather than the (possibly older) copy
# packed into the bundle: unpack it and overlay airgap/ from the checkout.
if [[ ${USE_BUNDLED_TOOLING:-0} != 1 ]]; then
    mkdir -p /tmp/bundle && tar -xzf "$BUNDLE" -C /tmp/bundle
    b=$(ls -d /tmp/bundle/*/)
    rm -rf "$b/airgap" && mkdir -p "$b/airgap"
    cp -a "$REPO/scripts/hermes-airgap" "$b/airgap/scripts"
    cp -a "$REPO/configs/hermes-airgap" "$b/airgap/configs"
    cp -a "$REPO/tests/hermes-airgap" "$b/airgap/tests"
    [[ -d $REPO/docs/hermes-airgap ]] && cp -a "$REPO/docs/hermes-airgap" "$b/airgap/docs"
    BUNDLE=$b
fi
"$REPO/scripts/hermes-airgap/install-server.sh" --bundle "$BUNDLE" --wheelhouse --skip-apt \
    --llm-base-url http://127.0.0.1:8000/v1 --llm-model mock-hermes --context-length 131072

step "preflight"
hermes-preflight

step "add users"
for u in alice bob; do
    ssh-keygen -q -t ed25519 -N '' -f "/root/$u.key" -C "$u-test"
    hermes-add-user "$u" --pubkey "/root/$u.key.pub"
done

step "start backends (what hermes-serve@.service does)"
start_backend() {
    local u=$1
    # shellcheck disable=SC1090
    ( set -a; . "/etc/hermes/users/$u.env"; set +a
      cd "/home/$u/work"
      exec runuser -u "$u" -- env HOME="/home/$u" HERMES_HOME="/home/$u/.hermes" \
          HERMES_PORT="$HERMES_PORT" HERMES_DASHBOARD_SESSION_TOKEN="$HERMES_DASHBOARD_SESSION_TOKEN" \
          hermes serve --host 127.0.0.1 --port "$HERMES_PORT" ) >"/tmp/serve-$u.log" 2>&1 &
}
start_backend alice
start_backend bob
for _ in $(seq 1 60); do
    ss -Hltn 'sport = :9121' | grep -q . && ss -Hltn 'sport = :9122' | grep -q . && break
    sleep 1
done
ss -Hltn | grep -E ':912[12]' || { tail -50 /tmp/serve-*.log; exit 1; }

step "smoke test alice (bob as the other user)"
hermes-smoke-test alice --other-user bob

step "SSH tunnel: what Start-HermesTunnel.ps1 does from Windows"
/usr/sbin/sshd
sleep 1
ALICE_TOKEN=$(sed -n 's/^HERMES_DASHBOARD_SESSION_TOKEN=//p' /etc/hermes/users/alice.env)
SSHOPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ExitOnForwardFailure=yes -N)
ssh "${SSHOPTS[@]}" -i /root/alice.key -L 127.0.0.1:19119:127.0.0.1:9121 alice@127.0.0.1 &
TUN=$!
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Hermes-Session-Token: $ALICE_TOKEN" http://127.0.0.1:19119/api/sessions)
echo "alice via tunnel, gated API with token -> $code"
[[ $code == 200 ]] || { echo "FAIL: tunnel"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:19119/api/sessions)
echo "alice via tunnel, gated API without token -> $code"
[[ $code == 401 ]] || { echo "FAIL: token not enforced"; exit 1; }
kill $TUN

# bob tries to tunnel to alice's port: sshd PermitOpen refuses the forward.
ssh "${SSHOPTS[@]}" -i /root/bob.key -L 127.0.0.1:19120:127.0.0.1:9121 bob@127.0.0.1 &
TUN=$!
sleep 2
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "X-Hermes-Session-Token: $ALICE_TOKEN" http://127.0.0.1:19120/api/sessions || true)
kill $TUN 2>/dev/null || true
echo "bob tunnelling to alice's port -> HTTP $code"
[[ $code == 000 ]] || { echo "FAIL: bob reached alice's backend"; exit 1; }

step "leak check: no egress attempts outside the allow-list during an agent turn + backend restart"
egress_packets() {
    nft list chain inet hermes output | awk '/hermes-egress/ { for (i = 1; i <= NF; i++) if ($i == "packets") print $(i + 1) }'
}
before=$(egress_packets)
pkill -u alice -f 'hermes serve' || true
sleep 1
start_backend alice
for _ in $(seq 1 60); do ss -Hltn 'sport = :9121' | grep -q . && break; sleep 1; done
(cd /home/alice/work && runuser -u alice -- env HOME=/home/alice hermes -z "Run: ls -la with the terminal tool." >/dev/null 2>&1)
denied=$(( $(egress_packets) - before ))
nft list chain inet hermes output | grep counter
if [[ -z ${E2E_NETWORK:-} ]]; then
    echo "SKIP: --network none has no route, so blocked attempts never reach nftables (run with E2E_NETWORK=1 on a network-attached container)"
elif [[ $denied == 0 ]]; then
    echo "PASS: 0 egress attempts outside the allow-list"
else
    echo "FAIL: $denied packet(s) hit the egress deny rule"; exit 1
fi

step "ALL CHECKS PASSED"
