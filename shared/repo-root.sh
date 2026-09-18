#!/usr/bin/env bash
# Canonical repo-root resolver for shell entry points.
#
# Source this file to get VAULT_ROOT: the absolute, physical path to this
# repository's root, derived from this file's own location. A clone works from
# any directory under any username, so no wrapper needs a hardcoded default.
#
#   VAULT_ROOT       explicit absolute directory override (must already exist)
#   CHRONO_VAULT_ROOT unrelated — the out-of-repo private vault sentinel
#
# Overrides retain their spelling for prefix-based containment checks. A root
# differing from this checkout is diagnosed on stderr; unset VAULT_ROOT to use
# this checkout. Fixture/data-only roots remain supported, without repo markers.
# Initialization failures exit the noninteractive caller, including callers
# without errexit or sourcing inside a conditional. There is no failure opt-out.
# Interactive sourcing is refused; run a child bash to handle its exit status.
#
# Callers resolve their own symlinks BEFORE sourcing, because a wrapper invoked
# through a symlink (bin/squad is documented as `ln -s <repo>/bin/squad
# ~/.local/bin/squad`) has a BASH_SOURCE pointing outside the repo. The standard
# two-line preamble is:
#
#   # shellcheck source=../shared/repo-root.sh
#   source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" \
#       2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

case $- in
    *i*)
        printf 'repo-root.sh: interactive sourcing is unsupported; run a child bash\n' >&2
        return 1
        ;;
esac

# Follow every symlink hop in a path and echo the physical result. Uses plain
# readlink, without depending on the newer BSD/macOS `readlink -f` option.
vs_resolve_symlink() {
    local target="$1" link dir hops=0
    while [[ -L "${target}" ]]; do
        hops=$((hops + 1))
        if (( hops > 40 )); then
            printf 'repo-root.sh: symlink loop resolving %s\n' "$1" >&2
            return 1
        fi
        link="$(readlink -- "${target}")" || {
            printf 'repo-root.sh: readlink failed resolving %s\n' "${target}" >&2
            return 1
        }
        case "${link}" in
            /*) target="${link}" ;;
            *)  target="$(dirname -- "${target}")/${link}" ;;
        esac
    done
    dir="$(CDPATH='' cd -- "$(dirname -- "${target}")" && pwd -P)" || return 1
    printf '%s/%s\n' "${dir}" "$(basename -- "${target}")"
}

# Always derive the location, even with an override, so inherited roots cannot
# redirect a worktree invocation silently. Physical paths are for comparison
# only; the public override retains its spelling (notably /var on macOS).
_vs_self="$(vs_resolve_symlink "${BASH_SOURCE[0]}")" || exit 1
_vs_derived="$(CDPATH='' cd -- "$(dirname -- "${_vs_self}")/.." && pwd -P)" || exit 1

if [[ -n "${VAULT_ROOT:-}" ]]; then
    # An override is used verbatim, never canonicalised. Callers test whether a
    # path is inside the vault by string prefix, and on macOS `pwd -P` rewrites
    # /var to /private/var -- which would make every such comparison miss.
    if [[ "${VAULT_ROOT}" != /* ]]; then
        printf 'repo-root.sh: VAULT_ROOT must be an absolute path: %s\n' "${VAULT_ROOT}" >&2
        exit 1
    fi
    if [[ ! -d "${VAULT_ROOT}" ]]; then
        printf 'repo-root.sh: VAULT_ROOT is not a directory: %s\n' "${VAULT_ROOT}" >&2
        exit 1
    fi
    _vs_override="$(CDPATH='' cd -- "${VAULT_ROOT}" && pwd -P)" || exit 1
    if [[ "${_vs_override}" != "${_vs_derived}" ]]; then
        printf 'repo-root.sh: warning: VAULT_ROOT=%s overrides location-derived root %s; unset VAULT_ROOT to use this checkout\n' \
            "${VAULT_ROOT}" "${_vs_derived}" >&2
    fi
else
    VAULT_ROOT="${_vs_derived}"
fi

unset _vs_self _vs_derived _vs_override
export VAULT_ROOT
