#!/bin/sh
# /usr/local/bin/hermes — launcher for the shared air-gapped install.
#
# The code lives in $HERMES_ROOT/hermes-agent (a plain source tree, no .git),
# the Python 3.14 venv in $HERMES_ROOT/venv and native tools (ripgrep, uv)
# in $HERMES_ROOT/tools/bin. /opt/hermes/current points at the active release.
HERMES_ROOT=${HERMES_ROOT:-/opt/hermes/current}
export HERMES_DISABLE_LAZY_INSTALLS=1
export PATH="$HERMES_ROOT/tools/bin:$PATH"
if [ -z "${HERMES_HOME:-}" ]; then
    HERMES_HOME="$HOME/.hermes"
    export HERMES_HOME
fi
exec "$HERMES_ROOT/venv/bin/python" "$HERMES_ROOT/hermes-agent/hermes" "$@"
