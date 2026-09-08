#!/usr/bin/env bash
# Thin Claude Code hook entrypoint. It remains inert until settings wire it.

set -uo pipefail

focus_gate_warning() {
    local detail="$1"
    printf '%s\n' '{"systemMessage":"FOCUS GATE WARNING — fail open: hook entrypoint failed; see stderr"}'
    printf 'FOCUS GATE WARNING — fail open: %s\n' "$detail" >&2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || {
    focus_gate_warning "could not resolve script directory"
    exit 0
}
repo_root="$(cd -- "${script_dir}/.." && pwd -P)" || {
    focus_gate_warning "could not resolve repository root"
    exit 0
}

# Board workers share the coordinator's user and repository hooks. This guard
# must run before sourcing helpers or launching Python, so a worker sees a true
# no-op even if the hook's dependencies are broken.
if ! physical_cwd="$(pwd -P 2>/dev/null || pwd)"; then
    focus_gate_warning "could not determine the hook working directory"
    exit 0
fi
for guard_path in "${physical_cwd}" "${CLAUDE_PROJECT_DIR:-}"; do
    case "/${guard_path#/}/" in
        */_state/board-worktrees/*) exit 0 ;;
    esac
done

pane_helper="${CHRONO_FOCUS_GATE_PANE_HELPER:-${repo_root}/shared/chrono-pane.sh}"
if ! source "${pane_helper}" >/dev/null 2>&1; then
    focus_gate_warning "could not load ${pane_helper}"
    exit 0
fi
if ! declare -F chrono_pane_has_coordinator >/dev/null; then
    focus_gate_warning "${pane_helper} did not define chrono_pane_has_coordinator"
    exit 0
fi

# A hook child inherits TMUX_PANE from the coordinator. The shared helper looks
# at the pane shell's foreground child (the parent Claude CLI), so the hook
# subprocess does not make the probe self-fulfilling.
if [[ -z "${TMUX_PANE:-}" ]] || ! chrono_pane_has_coordinator "${TMUX_PANE}"; then
    exit 0
fi

python_bin="${CHRONO_FOCUS_GATE_PYTHON:-${repo_root}/.venv/bin/python}"
ledger="${CHRONO_FOCUS_GATE_LEDGER:-${repo_root}/_state/chrono/OPEN-WORK.md}"
if [[ ! -x "${python_bin}" ]]; then
    focus_gate_warning "Python interpreter is unavailable: ${python_bin}"
    exit 0
fi

hook_output="$(
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="${repo_root}/scripts/python${PYTHONPATH:+:${PYTHONPATH}}" \
    "${python_bin}" -m chrono_state.focus_gate --ledger "${ledger}"
)"
hook_status=$?
if [[ "${hook_status}" -ne 0 ]]; then
    focus_gate_warning "Python hook exited ${hook_status}"
    exit 0
fi

if [[ -n "${hook_output}" ]]; then
    printf '%s\n' "${hook_output}"
fi
exit 0
