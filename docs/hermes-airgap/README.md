# Hermes Agent in an air-gapped network — runbook

This runbook puts [Hermes Agent](https://github.com/NousResearch/hermes-agent) **v0.22.2** on an air-gapped **Debian 13 (amd64)** server, used by a few people from air-gapped **Windows 11** workstations through **Hermes Desktop**:

- The agent and its terminal and file tools run on the server. Each person has their own Linux account.
- Desktop is only the UI. It reaches the person's backend through an **SSH tunnel**.
- The only model the agent talks to is your **internal OpenAI-compatible LLM endpoint**.

```
Windows workstation (alice)                     Debian 13 server
┌──────────────────────────────┐  SSH :22  ┌─────────────────────────────────────────────┐
│ Hermes Desktop (MSIX)        │──────────▶│ sshd: Match User alice                      │
│   Remote gateway             │ -L 9119 → │       PermitOpen 127.0.0.1:9121             │
│   http://127.0.0.1:9119      │ 127.0.0.1 │            │                                │
│   + session token            │   :9121   │            ▼                                │
│ ssh.exe (Start-HermesTunnel) │           │ hermes-serve@alice  (User=alice, sandboxed) │
└──────────────────────────────┘           │   hermes serve --host 127.0.0.1 --port 9121 │
                                           │   HERMES_HOME=/home/alice/.hermes           │
                                           │   terminal: local, cwd /home/alice/work     │
                                           │            │                                │
                                           │            ▼                                │
                                           │   internal LLM  (only allowed egress)       │
                                           └─────────────────────────────────────────────┘
```

## How users are kept apart

| Layer | What it enforces |
|---|---|
| One Linux account per person | Tools run as that person. Each has their own `~/.hermes` (config, sessions, memory, skills) and `~/work`. Home directories are mode 0750. |
| Loopback-only backend | `hermes serve` binds to `127.0.0.1:<port>`, so nothing is exposed on the LAN. |
| sshd `PermitOpen` | Each user can forward only to their own port. No remote forwards, X11 or agent forwarding. `--tunnel-only` also removes shell access. |
| nftables `port_owner` | A new connection to port N on 127.0.0.1 is rejected unless the socket belongs to N's owner. The sshd child doing the forward runs as the logged-in user, so this also covers tunnels. |
| Session token | Every gated `/api/*` call needs `X-Hermes-Session-Token`. Each user gets their own random token, which systemd passes in from `/etc/hermes/users/<user>.env` (root, mode 0600). |
| nftables egress allow-list | Processes of Hermes users can reach only loopback, the LLM endpoint and DNS (`/etc/hermes/egress.conf`). Everything else is logged and rejected. |
| systemd sandbox | `ProtectSystem=strict`, `NoNewPrivileges` (no sudo), writes allowed only under the user's home and a private `/tmp`. |

## What's in this repo

```
scripts/hermes-airgap/
  build-bundle.sh        connected side: builds the offline bundle
  install-server.sh      server: install / upgrade / --activate (rollback)
  preflight.sh           server: OS, LLM endpoint, context, tool-calling, mirror checks
  add-user.sh            server: per-user account, port, token, sshd, firewall, service
  remove-user.sh
  smoke-test.sh          server: end-to-end check for one user
  firewall.sh            server: reload | status | mode enforce|log | denied
  lib.sh                 shared helpers
  windows/Install-HermesDesktop.ps1   workstation: verify + sideload MSIX, remote settings
  windows/Start-HermesTunnel.ps1      workstation: key generation, tunnel, logon task
configs/hermes-airgap/   config.yaml / .env templates, systemd units, nftables template, launcher
tests/hermes-airgap/     mock_llm.py (OpenAI-compatible stub), container-e2e.sh (offline test)
docs/hermes-airgap/      this runbook, upstream-notes.md, windows-client.md
```

---

## 1. Build the bundle (connected machine)

Use any connected Debian 13 amd64 host or container that has `git curl python3 ca-certificates`:

```bash
docker run --rm -it -v "$PWD:/repo:ro" -v "$PWD/out:/out" debian:13 bash -c '
  apt-get update && apt-get install -y --no-install-recommends git curl python3 ca-certificates &&
  /repo/scripts/hermes-airgap/build-bundle.sh --out /out'
```

| Option | Meaning |
|---|---|
| `--ref v0.22.2` | Hermes tag or commit to package. |
| `--with-wheelhouse` | Also ship all 117 Python packages, as exact files whose hashes still match. Use this if your PyPI mirror lacks some of them. Adds about 70 MB. |
| `--skip-desktop` | Leave out the Windows package (about **4.3 GB**). |
| `--msix-url URL` | A specific Desktop build. The default is the current stable App Installer feed. |

**Output** in `out/`:

- `hermes-airgap-server-v0.22.2.tar.gz`, about 230 MB with the wheelhouse. It contains:
  - the source tree without `.git`
  - CPython 3.14.7, uv 0.12.3 and ripgrep 15.2.0, each checked against the SHA-256 that upstream pins in `pm/lock.json`
  - a hash-pinned `requirements.txt`
  - these scripts
- `windows/`: `HermesBundled-*.msixbundle`, the two PowerShell scripts and `windows-client.md`.
- `MANIFEST.json` (versions, upstream URLs and hashes) and `SHA256SUMS`.

Carry the whole `out/` directory across the air gap, then verify it with `sha256sum -c SHA256SUMS`.

> Why not upstream's `install.sh` or `hermes pm bundle`? `install.sh` has no offline mode. `pm bundle` packs **every** optional tool for the platform: Chromium, llama.cpp CUDA/ROCm/Vulkan builds, gh, bws and others. It also ignores your PyPI mirror, and upstream doesn't test it on Linux. See [upstream-notes.md](upstream-notes.md).

## 2. Install the server

The server needs `ca-certificates curl python3 tar gzip openssh-server nftables iproute2 procps sqlite3`; the script installs them from your Debian mirror. Run as root:

```bash
tar -xzf hermes-airgap-server-v0.22.2.tar.gz
cd hermes-airgap-server-v0.22.2/airgap/scripts

./preflight.sh --llm-base-url http://llm.internal:8000/v1 --llm-model Hermes-4-70B \
               --pypi-index https://nexus.internal/repository/pypi/simple \
               --requirements ../../requirements.txt

./install-server.sh --bundle ../../../hermes-airgap-server-v0.22.2.tar.gz \
    --pypi-index https://nexus.internal/repository/pypi/simple \
    --llm-base-url http://llm.internal:8000/v1 --llm-model Hermes-4-70B \
    --context-length 131072
```

- Use `--wheelhouse` instead of `--pypi-index` if you built the bundle with `--with-wheelhouse`.
- `--egress 10.1.2.3,10.9.0.0/24` adds more allowed destinations. By default only the LLM host's addresses and your DNS resolvers are allowed.
- `--egress-mode log` only logs blocked traffic instead of rejecting it. Useful during commissioning; `hermes-firewall mode enforce` switches back.

**LLM endpoint requirements:**
- It must serve **at least 64K context**; Hermes refuses tool use below that.
- It must return structured `tool_calls`. For vLLM with Hermes models: `--enable-auto-tool-choice --tool-call-parser hermes --max-model-len 131072`.
- `preflight.sh` checks both.

## 3. Add users

For each person, have them run `Start-HermesTunnel.ps1 -NewKey` on their workstation (step 4) and send you the public key. Then:

```bash
hermes-add-user alice --pubkey /tmp/alice.pub            # optional: --tunnel-only, --ask-llm-key
hermes-smoke-test alice --other-user bob                  # once two users exist
```

`add-user` prints exactly what the person needs: the tunnel command with their port, and the Desktop URL and token. The same details are saved in their `~/.hermes/desktop-connection.txt`.

**Re-running for an existing user** keeps their port, token and config. Two flags change that:
- `--rotate-token` issues a new token.
- `--reconfigure` re-renders `config.yaml` and keeps a `.bak` copy.

**Removing a user:**
- `hermes-remove-user alice` stops the backend and keeps the data.
- `--delete-account` also deletes the account and home directory, after a confirmation prompt.

## 4. Windows workstations

See [windows-client.md](windows-client.md), which is also copied into `out/windows/`. In short:
1. `Install-HermesDesktop.ps1 -Token <token>` verifies the hash and signature, then sideloads the MSIX. Optionally add `-BlockInternet`.
2. `Start-HermesTunnel.ps1 -Server <server> -User alice -RemotePort 9121` (add `-RegisterLogonTask` to start it at every logon).
3. Start Hermes Desktop. It connects to `http://127.0.0.1:9119`, and from there to your backend.

## 5. Day-2 operations

| Task | Command |
|---|---|
| Service status / logs | `systemctl status hermes-serve@alice`, `journalctl -u hermes-serve@alice` |
| Restart everyone | `systemctl restart 'hermes-serve@*'` |
| Health check | `hermes-preflight`, `hermes-smoke-test <user>` |
| Blocked traffic | `hermes-firewall denied`, `hermes-firewall status` |
| Allow another destination | edit `/etc/hermes/egress.conf`, then `hermes-firewall reload` |
| Change model / endpoint for everyone | edit `/etc/hermes/site.conf`, then `hermes-add-user <u> --reconfigure` for each user |
| Change one user's model | `hermes-add-user alice --model other-model --reconfigure` |

### Upgrade and rollback

```bash
# build a new bundle with --ref vX.Y.Z on the connected side, carry it over, then:
./install-server.sh --bundle hermes-airgap-server-vX.Y.Z.tar.gz --pypi-index ...
#   -> installs /opt/hermes/vX.Y.Z next to the old release
#   -> stops each backend, backs up ~/.hermes/state.db* + memories/ to /var/backups/hermes/<timestamp>/
#   -> flips /opt/hermes/current, restarts hermes-serve@*

./install-server.sh --activate v0.22.2      # roll back (same backup + restart)
```

Upgrade the Windows workstations with the matching Desktop MSIX by re-running `Install-HermesDesktop.ps1`, and re-run `-BlockInternet` after each upgrade. To restore a user's data, stop their service and copy the backed-up `state.db*` and `memories/` back into `~/.hermes/`.

## 6. Verification performed

All of the following ran in Debian 13 containers against the real v0.22.2 bundle, with `mock_llm.py` standing in for the LLM:

- **`tests/hermes-airgap/container-e2e.sh`** with `--network none` (fully offline): install, preflight, two users, smoke test, a real SSH tunnel, and bob's forward to alice's port being refused.
  - Again with a network attached and `E2E_NETWORK=1`: a backend restart plus an agent turn produced **0** packets on the egress deny rule.
- **A systemd-enabled container**:
  - `hermes-serve@` units start and pass the smoke test.
  - An agent turn works under the unit's sandbox properties.
  - Re-running `add-user` keeps the token.
  - `remove-user`, `--activate`, firewall mode switching and tunnel-only accounts all behave as intended.
  - `sshd -T` confirms the per-user `PermitOpen`.
- **Static checks:** `shellcheck -S warning` passes on all shell scripts. PowerShell parses both `.ps1` files with no errors; PSScriptAnalyzer reports only intentional `Write-Host` use and parameter-set switches.

**Not verified here** (there was no Windows machine and no real LLM):
- Installing the MSIX, the signature check while offline, and the Desktop UI connecting through the tunnel.
- Tool-call quality with your real model.

Check these with the Windows steps in [windows-client.md](windows-client.md), and by running `hermes-smoke-test` against the real endpoint.

Re-run the container test:

```bash
docker run --rm --network none --cap-add NET_ADMIN -v "$PWD:/repo:ro" -v "$PWD/out:/out:ro" \
    hermes-airgap-testbase /repo/tests/hermes-airgap/container-e2e.sh
```

(The header of `container-e2e.sh` shows how to build `hermes-airgap-testbase`.)

## Out of scope / possible extensions

- **Not configured:** messaging gateways and cron (both need `hermes gateway` per user), the OpenAI-compatible API server on port 8642, browser and computer-use tools, MCP servers (`mcp_servers:` works if you add internal ones), the Docker terminal sandbox, speech-to-text and text-to-speech, and TLS/OIDC in front of the backend.
- **Desktop's built-in SSH mode** is an alternative to the tunnel script. Desktop SSHes in itself and starts a temporary `hermes serve --port 0` (`/usr/local/bin/hermes` is on PATH for it). That mode needs a random port, so it is incompatible with the fixed-port `PermitOpen` isolation used here. See [upstream-notes.md](upstream-notes.md).
