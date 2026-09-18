#!/usr/bin/env bash
# Sources operator secrets (VIBESQUAD_DAEMON_TOKEN + others) before launching
# the daemon. launchd doesn't inherit shell env, so this wrapper is required.
set -euo pipefail

# Source secrets so VIBESQUAD_DAEMON_TOKEN and other env vars are available
if [[ -f "$HOME/.config/shell/secrets.zsh" ]]; then
    # The secrets file is a zsh file the operator edits by hand. Under this
    # script's `set -u`, one bare `$UNSET_VAR` reference in it aborts the
    # launcher before Python starts and launchd crash-loops the daemon
    # (happened 2026-09-13 when GEMINI_API_KEY was removed but a line still
    # read it). Relax -u only for the source; restore it right after.
    # Dry-run the file under nounset in a throwaway subshell first: an unset
    # reference is reported loudly (it means some derived value is malformed)
    # without being allowed to take the daemon down (review F-04, replay).
    # shellcheck source=/dev/null
    if ! unset_report="$( (set -u; source "$HOME/.config/shell/secrets.zsh") 2>&1 >/dev/null )"; then
        echo "daemon-launcher: WARNING: secrets.zsh references unset variable(s); the daemon starts anyway but that derived value is malformed: ${unset_report}" >&2
    fi
    set +u
    # shellcheck source=/dev/null
    source "$HOME/.config/shell/secrets.zsh"
    set -u
fi

# The relaxed sourcing above means a typo in the secrets file now yields an
# empty value instead of an abort. Catch the one value this daemon cannot run
# without here, with the reason on stderr, rather than as a launchd crash loop
# whose only trace is daemon/auth.py refusing at construction time.
if [[ -z "${VIBESQUAD_DAEMON_TOKEN:-}" ]]; then
    echo "daemon-launcher: VIBESQUAD_DAEMON_TOKEN is empty after sourcing ~/.config/shell/secrets.zsh; refusing to start the daemon" >&2
    exit 1
fi

# Optional path overrides must be absent or nonempty. A typo expanded by the
# relaxed source otherwise bypasses Python's os.environ.get(name, default).
# Reject it here so the operator sees the bad name instead of a healthy daemon
# whose MCP registry silently points into the wrong directory.
for daemon_path_var in VIBE_SQUAD_ROOT VIBE_PLUGINS VIBE_PYTHON VIBESQUAD_STATE_DIR VAULT_ROOT; do
    if [[ "${!daemon_path_var+x}" == x && -z "${!daemon_path_var}" ]]; then
        echo "daemon-launcher: ${daemon_path_var} is empty after sourcing ~/.config/shell/secrets.zsh; unset it to use the default or set a nonempty path; refusing to start the daemon" >&2
        exit 1
    fi
done

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"
REPO="${VAULT_ROOT}"
cd "$REPO"

exec "$REPO/.venv/bin/python" -m daemon.main
