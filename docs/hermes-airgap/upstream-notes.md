# Upstream findings (Hermes Agent v0.22.2, commit d923a5e)

These are Phase 0 notes from reading the source at tag `v0.22.2`. They explain the design choices in the scripts and list every outbound connection we neutralise. Paths are relative to the hermes-agent repo.

## Runtime and packaging

**Python 3.14 is required.**
- `pyproject.toml` says `requires-python >=3.11,<3.15`.
- But every core dependency carries the marker `python_version >= '3.14'`, and `[tool.uv] environments` resolves only for 3.14.
- Debian 13 ships 3.13, so the bundle carries python-build-standalone **3.14.7+20260901**, the build pinned in `pm/lock.json`. It includes its own modern SQLite.

**The project cannot be built as a wheel.**
- `setup.py` refuses `bdist_wheel`/`sdist` unless `HERMES_NIX_BUILD=1`.
- A wheel would also be missing skills, locales, `optional-mcps` and plugin data.
- So, like upstream's Dockerfile: a plain source tree, a venv holding only the dependencies, and a launcher that runs `venv/bin/python hermes-agent/hermes`.

**Lock files.**
- The root `uv.lock` is authoritative. `pm/uv.lock` belongs to PM's own worker.
- `uv export --frozen --no-dev --extra all --no-emit-project` gives 117 hash-pinned packages, all with manylinux wheels (no source-only packages).
- The one URL dependency, `psutil @ git+…; sys_platform == 'android'`, is dropped.
- `[all]` = cron, pty, mcp, uvloop, sms, acp, google, web, youtube. That is what upstream's installer uses.

**A source tree without `.git` counts as a managed install.**
- Hermes does no self-update and no launch-time dependency sync on it.
- Lazy installs are also disabled with `HERMES_DISABLE_LAZY_INSTALLS=1` and `security.allow_lazy_installs: false`.

**Native tools** (`pm/lock.json`, linux-x64, each SHA-256 pinned):

| Tool | Version | Needed for the headless backend? |
|---|---|---|
| python | 3.14.7 | yes |
| uv | 0.12.3 | yes, to install the hashed requirements |
| ripgrep | 15.2.0 | yes: the file-search tool refuses broad searches without it |
| node / npm | 26.7.0 / 12.0.2 | no: `hermes serve` doesn't build the web UI |
| ffmpeg | 9.0.1 | no: media features only |
| agent-browser + chromium, cua-driver, gh, bws, iron-proxy, llama.cpp | various | no: browser, computer-use and similar optional features |

**Why not upstream's offline tooling:**
- `scripts/install.sh` has no offline mode; its uv download URL is hard-coded.
- `hermes pm bundle --out DIR --ref REF` builds a relocatable payload, but:
  - it includes **every** package in `lock.json` for the host platform (Chromium, CUDA/ROCm/Vulkan llama.cpp, …);
  - it can't use the internal PyPI mirror;
  - its Linux CI legs are disabled (`.github/workflows/pm-bundle.yml`).

## Backend (`hermes serve`) and Desktop

**What `hermes serve` is.**
- It is `hermes dashboard` in headless mode, a FastAPI/uvicorn server. Defaults: `--host 127.0.0.1 --port 9119`.
- It doesn't need Node and never builds or mounts the web UI.
- Agents run inside the server process. Desktop chat uses `/api/ws`.

**One backend per OS user.** A second `serve` for the same user attaches to the first and exits. The lock lives in `~/.local/state/hermes`, so separate Linux accounts each get their own backend.

**It does not start `hermes gateway`.** Cron and messaging platforms need a separate gateway process, which is out of scope here.

**Auth when bound to loopback.**
- No login; every gated `/api/*` call needs `X-Hermes-Session-Token: <token>` (or `Bearer`).
- The token is `HERMES_DASHBOARD_SESSION_TOKEN`; without it, a random token is generated on each start.
- A `.env` file never overrides an inherited token, which is why the systemd unit passes it in through the environment.
- Public paths: `/api/health`, `/api/status`, `/api/config/defaults`, `/api/config/schema`, `/api/model/info`, `/api/dashboard/themes`, `/api/dashboard/plugins`, `/api/cron/fire`. The smoke test therefore checks `/api/sessions`.
- Don't set `HERMES_DESKTOP=1` on these services; it relaxes loopback auth.

**Desktop remote mode.**
- Configured through **Settings → Gateways → Remote gateway** (URL + token), or `HERMES_DESKTOP_REMOTE_URL` together with `HERMES_DESKTOP_REMOTE_TOKEN`. The URL without the token is an error.
- In remote mode the local runtime bundled in the MSIX is **never started**.
- Desktop calls `/api/health` and `/api/status`, then `/api/ws?token=…` and REST with `X-Hermes-Session-Token`.

**Desktop's built-in SSH mode** (an alternative we don't use).
- Desktop runs the system `ssh.exe` with `BatchMode=yes` and finds `hermes` on the remote via `bash -lc 'command -v hermes'` (our `/usr/local/bin/hermes` qualifies).
- It then starts `HERMES_DESKTOP=1 hermes serve --isolated --host 127.0.0.1 --port 0 --ssh-session-token-file …` and forwards whatever random port that gets.
- We don't use it because a random port can't be restricted with fixed-port sshd `PermitOpen` or the nftables port-owner rule.

## Hermes Desktop on Windows

**Package.**
- Windows releases are **MSIX only**: `HermesBundled-<version>-win.msixbundle`, about 4.3 GB, Windows 11 22H2 or later.
- Signed through Azure Trusted Signing by `CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US`.
- Published to `https://hermes-assets.nousresearch.com/releases/tag/<tag>/`. The stable App Installer feed is `…/releases/win32/stable/stable.appinstaller`, which `build-bundle.sh` reads to find the bundle.
- Nothing is attached to GitHub releases.
- At the time of writing, the stable feed pointed to the `rc.3-v0.22.0` Desktop build (version `2026.6864.2132.0`). That build is the client; the agent version is set by the server (v0.22.2).

**`Hermes-Setup.exe`** is an online bootstrapper (it builds from source), so it can't be used here.

**Updates.**
- A bundled Windows build uses the `app-installer` updater.
- Sideloading with `Add-AppxPackage -Path` registers **no** App Installer source, so the update check returns `no-app-installer-source`.
- There is no switch to turn the check off, but a failed check does no harm.
- `-BlockInternet` adds Windows Firewall rules as defence in depth.

**Other on-demand outbound calls from the UI** (they fail harmlessly offline):
- Google Fonts, from the default `nous` theme.
- The plugin catalog and skills hub (`hermes-agent.nousresearch.com`).
- Nous Portal sign-in.

## Config keys used (`hermes_cli/config_defaults.py`)

| Key | Value | Why |
|---|---|---|
| `model.provider` / `base_url` / `default` / `key_env` / `context_length` | `custom`, internal URL, served id, `HERMES_LLM_API_KEY`, ≥ 65536 | An explicit `context_length` skips metadata probing (models.dev, OpenRouter). |
| `auxiliary.<19 tasks>.provider` | `main` | An unset task falls back to OpenRouter. |
| `agent.disabled_toolsets` | web, search, x_search, browser, computer_use, vision, image_gen, video, video_gen, tts | These need the internet or local media models. |
| `terminal.backend` / `terminal.cwd` | `local`, absolute `~/work` | `"."` would mean the service's working directory. |
| `web.keyless_fallback` / `keyless_rescue` | false | Stops the free-tier search rotation (Exa, Parallel, Firecrawl). |
| `browser.backend` | `"off"` | |
| `stt.enabled` | false | faster-whisper would download a model. |
| `updates.check` | false | Stops GitHub release checks, including one at import time. |
| `telemetry.shared_metrics.enabled` / `send` | false | Already the default. |
| `security.allow_lazy_installs` | false | |
| `auth.adopt_external_logins` | false | |
| `model_catalog.enabled` | false | Stops the remote model catalog for the picker. |
| `tools.connectors.enabled` | false | |
| `nous.guest` / `keepalive_interval_seconds` | false / 0 | Stops the Nous free tier and the Portal keepalive thread. |

**No global "offline" switch exists.** The nftables egress rule is the backstop: with it, a backend start plus an agent turn produced 0 blocked packets in testing.

## Other upstream facts worth knowing

- `serve` exits 78 on a host/port conflict with an already-running backend; the unit sets `RestartPreventExitStatus=78`.
- `GET /api/hermes/update/check` (the dashboard's "check for updates" button) ignores `updates.check`. Without `.git` it returns "not-a-git-checkout" and makes no network call.
- `hermes --version` prints `vunknown (<date>)` for a tree without `.git`. That's harmless; the actual version is in `/opt/hermes/current/MANIFEST.json`.
