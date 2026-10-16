#!/usr/bin/env bash
# Build the offline Hermes bundle on a CONNECTED Debian 13 amd64 machine
# (or inside `docker run --rm -it -v "$PWD:/work" -w /work debian:13`).
#
# Output (in --out DIR):
#   hermes-airgap-server-<ref>.tar.gz   server bundle (source, CPython 3.14,
#                                        uv, ripgrep, hashed requirements,
#                                        optional wheelhouse, these scripts)
#   windows/                             Hermes Desktop .msixbundle + PowerShell
#                                        scripts + end-user guide
#   MANIFEST.json, SHA256SUMS
#
# Python wheels are NOT shipped by default: the air-gapped server installs
# them from the internal PyPI mirror, pinned by hash. Use --with-wheelhouse
# to ship them too (for packages the mirror lacks).
#
# Every download is checked against a SHA-256 pinned upstream in
# pm/lock.json; nothing is piped into a shell.

set -euo pipefail

REF=v0.22.2
REPO_URL=https://github.com/NousResearch/hermes-agent.git
OUT=
WITH_WHEELHOUSE=0
WITH_DESKTOP=1
APPINSTALLER_URL=https://hermes-assets.nousresearch.com/releases/win32/stable/stable.appinstaller
MSIX_URL=
KEEP_WORK=0

usage() {
    cat <<EOF
Usage: $0 --out DIR [options]
  --ref REF              hermes-agent git tag/commit (default $REF)
  --repo URL             git URL (default $REPO_URL)
  --with-wheelhouse      also download every wheel/sdist into the bundle
  --skip-desktop         don't download the Windows Hermes Desktop package (~4.3 GB)
  --msix-url URL         Desktop .msixbundle URL (default: MainBundle of the
                         stable App Installer feed, $APPINSTALLER_URL)
  --keep-work            keep the temporary work directory
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --out) OUT=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --repo) REPO_URL=$2; shift 2 ;;
        --with-wheelhouse) WITH_WHEELHOUSE=1; shift ;;
        --skip-desktop) WITH_DESKTOP=0; shift ;;
        --msix-url) MSIX_URL=$2; shift 2 ;;
        --keep-work) KEEP_WORK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ -n $OUT ]] || { usage >&2; exit 2; }

log() { printf '\033[1;34m[build-bundle]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[build-bundle] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

for cmd in git curl python3 tar sha256sum; do
    command -v "$cmd" >/dev/null || die "missing '$cmd' (apt-get install git curl python3 tar coreutils ca-certificates)"
done
[[ $(uname -m) == x86_64 ]] || die "build on x86_64 — the bundle's wheels/binaries must match the amd64 target"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/hermes-bundle.XXXXXX")
[[ $KEEP_WORK == 1 ]] || trap 'rm -rf "$WORK"' EXIT
NAME="hermes-airgap-server-$REF"
STAGE="$WORK/$NAME"
mkdir -p "$STAGE/artifacts"

# --- 1. source -----------------------------------------------------------
log "cloning $REPO_URL at $REF"
git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$WORK/src" 2>/dev/null \
    || { git clone --quiet "$REPO_URL" "$WORK/src" && git -C "$WORK/src" checkout --quiet "$REF"; }
COMMIT=$(git -C "$WORK/src" rev-parse HEAD)
log "commit $COMMIT"

# Plain source tree without .git (Hermes treats a tree without .git as a
# managed install: no self-update, no launch-time dependency sync). Drop
# what the headless backend never uses — same idea as upstream's payload.
mkdir -p "$STAGE/hermes-agent"
git -C "$WORK/src" archive --format=tar HEAD | tar -x -C "$STAGE/hermes-agent"
rm -rf "$STAGE/hermes-agent"/{tests,website,apps,ui-tui,web,.github}

# --- 2. pinned native artefacts from pm/lock.json ---------------------------
python3 - "$WORK/src/pm/lock.json" >"$WORK/artifacts.tsv" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))["packages"]
# python: interpreter for the venv; uv: installs the hashed requirements;
# ripgrep: Hermes' file-search tool refuses broad searches without it.
for name in ("python", "uv", "ripgrep"):
    pkg = lock[name]
    art = pkg["artifacts"]["linux-x64"]
    print(name, pkg.get("version", ""), art["url"], art["sha256"], sep="\t")
PY

while IFS=$'\t' read -r name version url sha; do
    file="$STAGE/artifacts/$(basename "$url")"
    log "downloading $name $version"
    curl -fsSL --retry 4 -o "$file" "$url"
    echo "$sha  $file" | sha256sum -c --quiet - || die "checksum mismatch for $url"
done <"$WORK/artifacts.tsv"
cp "$WORK/artifacts.tsv" "$STAGE/artifacts/artifacts.tsv"

# Unpack uv + python locally (needed for export / wheelhouse).
UV_TGZ=$(awk -F'\t' '$1=="uv"{print $3}' "$WORK/artifacts.tsv")
PY_TGZ=$(awk -F'\t' '$1=="python"{print $3}' "$WORK/artifacts.tsv")
mkdir -p "$WORK/uv" "$WORK/py"
tar -xzf "$STAGE/artifacts/$(basename "$UV_TGZ")" -C "$WORK/uv" --strip-components=1
tar -xzf "$STAGE/artifacts/$(basename "$PY_TGZ")" -C "$WORK/py" --strip-components=1
UV="$WORK/uv/uv"
PY="$WORK/py/bin/python3"

# --- 3. hashed requirements from upstream uv.lock ------------------------
# [all] = cron, pty, mcp, uvloop, sms, acp, google, web, youtube — what the
# official installer uses. The project itself is not a wheel (setup.py
# refuses bdist), so it is excluded and run from the source tree.
log "exporting hashed requirements (extra: all)"
(cd "$WORK/src" && UV_NO_CONFIG=1 "$UV" export --frozen --no-dev --extra all \
    --no-emit-project --no-header --format requirements-txt -o "$WORK/requirements.raw.txt" >/dev/null)
# Drop the Android-only git dependency (psutil @ git+...), which cannot be
# hash-pinned and never applies on Linux.
python3 - "$WORK/requirements.raw.txt" "$STAGE/requirements.txt" <<'PY'
import sys
src, dst = sys.argv[1:]
blocks, cur = [], []
for line in open(src):
    if line.startswith((" ", "\t")) and cur:
        cur.append(line)
    else:
        if cur:
            blocks.append(cur)
        cur = [line]
if cur:
    blocks.append(cur)
kept = [b for b in blocks if not (" @ git+" in b[0] or " @ http" in b[0])]
dropped = [b[0].split(";")[0].strip() for b in blocks if b not in kept]
open(dst, "w").writelines(l for b in kept for l in b)
for d in dropped:
    print("dropped non-hashable requirement:", d, file=sys.stderr)
PY
REQ_COUNT=$(grep -cE '^[A-Za-z0-9]' "$STAGE/requirements.txt")
log "$REQ_COUNT pinned packages"

# --- 4. optional wheelhouse (exact files, so hashes still match) ---------
if [[ $WITH_WHEELHOUSE == 1 ]]; then
    log "downloading wheelhouse"
    "$PY" -m pip download --quiet --disable-pip-version-check --no-deps \
        --require-hashes -r "$STAGE/requirements.txt" -d "$STAGE/wheelhouse"
fi
# Report sdist-only packages: the server then needs a compiler + headers.
SDISTS=$("$PY" - "$WORK/src/uv.lock" "$STAGE/requirements.txt" <<'PY'
import re, sys, tomllib
lock = tomllib.load(open(sys.argv[1], "rb"))
wanted = {m.group(1).lower().replace("_", "-"): m.group(2)
          for m in re.finditer(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)", open(sys.argv[2]).read(), re.M)}
out = []
for p in lock.get("package", []):
    n = p["name"].lower().replace("_", "-")
    if wanted.get(n) == p.get("version") and not p.get("wheels"):
        out.append(f"{p['name']}=={p['version']}")
print(" ".join(sorted(out)))
PY
)
[[ -n $SDISTS ]] && log "sdist-only packages (need build tools on the server): $SDISTS"

# --- 5. our own tooling ----------------------------------------------------
mkdir -p "$STAGE/airgap"
cp -a "$REPO_ROOT/scripts/hermes-airgap" "$STAGE/airgap/scripts"
cp -a "$REPO_ROOT/configs/hermes-airgap" "$STAGE/airgap/configs"
cp -a "$REPO_ROOT/tests/hermes-airgap" "$STAGE/airgap/tests"
cp -a "$REPO_ROOT/docs/hermes-airgap" "$STAGE/airgap/docs" 2>/dev/null || true
rm -rf "$STAGE/airgap/scripts/windows"

# --- 6. Windows: Hermes Desktop -------------------------------------------
DESKTOP_JSON=null
if [[ $WITH_DESKTOP == 1 ]]; then
    mkdir -p "$OUT/windows"
    if [[ -z $MSIX_URL ]]; then
        log "reading App Installer feed $APPINSTALLER_URL"
        MSIX_URL=$(curl -fsSL --retry 4 "$APPINSTALLER_URL" | python3 -c '
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
for el in root.iter():
    if el.tag.endswith("MainBundle") or el.tag.endswith("MainPackage"):
        print(el.attrib["Uri"]); break')
        [[ -n $MSIX_URL ]] || die "could not find MainBundle in $APPINSTALLER_URL"
    fi
    MSIX_FILE="$OUT/windows/$(basename "${MSIX_URL%%\?*}")"
    log "downloading Hermes Desktop $(basename "$MSIX_FILE") (large; resumable)"
    curl -fL --retry 8 -C - -o "$MSIX_FILE" "$MSIX_URL"
    cp "$REPO_ROOT"/scripts/hermes-airgap/windows/*.ps1 "$OUT/windows/"
    cp "$REPO_ROOT/docs/hermes-airgap/windows-client.md" "$OUT/windows/" 2>/dev/null || true
    DESKTOP_JSON=$(python3 -c 'import json,sys; print(json.dumps({"url": sys.argv[1], "file": "windows/" + sys.argv[2]}))' \
        "$MSIX_URL" "$(basename "$MSIX_FILE")")
fi

# --- 7. manifest, tarball, checksums ---------------------------------------
python3 - "$STAGE/MANIFEST.json" "$REF" "$COMMIT" "$WORK/artifacts.tsv" "$REQ_COUNT" \
    "$WITH_WHEELHOUSE" "$SDISTS" "$DESKTOP_JSON" <<'PY'
import json, sys, datetime
out, ref, commit, tsv, nreq, wheel, sdists, desktop = sys.argv[1:]
arts = [dict(zip(("name", "version", "url", "sha256"), l.rstrip("\n").split("\t"))) for l in open(tsv)]
json.dump({
    "hermes_agent": {"ref": ref, "commit": commit},
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "target": "debian-13-amd64",
    "artifacts": arts,
    "python_requirements": {"count": int(nreq), "extras": ["all"], "wheelhouse": wheel == "1",
                            "sdist_only": sdists.split()},
    "desktop": json.loads(desktop),
}, open(out, "w"), indent=2)
PY
cp "$STAGE/MANIFEST.json" "$OUT/MANIFEST.json"

log "packing $NAME.tar.gz"
tar -C "$WORK" -czf "$OUT/$NAME.tar.gz" "$NAME"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs -d '\n' sha256sum >SHA256SUMS)

log "done:"
(cd "$OUT" && find . -type f -printf '    %P  (%s bytes)\n' | sort >&2)
log "carry the whole $OUT directory across the air gap; verify with: sha256sum -c SHA256SUMS"
