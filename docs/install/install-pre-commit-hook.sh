#!/bin/bash
# Install the reviewed private-memory guard outside the mutable worktree.

set -euo pipefail

managed_marker="vibe-squad-managed-pre-commit/v1"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "install-pre-commit-hook: run this inside a Git checkout" >&2
    exit 1
}
source_guard="${repo_root}/scripts/hooks/pre-commit"
if [[ ! -f "${source_guard}" ]]; then
    echo "install-pre-commit-hook: missing reviewed guard: ${source_guard}" >&2
    exit 1
fi

git_common_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-common-dir)"
hooks_dir="${git_common_dir}/hooks"
entrypoint="${hooks_dir}/pre-commit"
installed_guard="${hooks_dir}/vibe-squad-pre-commit"
mkdir -p "${hooks_dir}"

if [[ -e "${entrypoint}" ]] && ! grep -Fq "${managed_marker}" "${entrypoint}"; then
    echo "install-pre-commit-hook: refusing to replace existing hook: ${entrypoint}" >&2
    echo "Review and compose that hook manually, then rerun this installer." >&2
    exit 1
fi

guard_tmp="$(mktemp "${hooks_dir}/.vibe-squad-pre-commit.XXXXXX")"
entrypoint_tmp="$(mktemp "${hooks_dir}/.pre-commit.XXXXXX")"
cleanup() {
    rm -f "${guard_tmp}" "${entrypoint_tmp}"
}
trap cleanup EXIT

install -m 0755 "${source_guard}" "${guard_tmp}"
cat > "${entrypoint_tmp}" <<'HOOK'
#!/bin/sh
# vibe-squad-managed-pre-commit/v1
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec python3 "${hook_dir}/vibe-squad-pre-commit"
HOOK
chmod 0755 "${entrypoint_tmp}"

mv -f "${guard_tmp}" "${installed_guard}"
mv -f "${entrypoint_tmp}" "${entrypoint}"
trap - EXIT

set +e
git -C "${repo_root}" config --local --unset-all core.hooksPath
unset_status=$?
set -e
if [[ "${unset_status}" -ne 0 && "${unset_status}" -ne 5 ]]; then
    echo "install-pre-commit-hook: installed hook but could not clear core.hooksPath" >&2
    exit "${unset_status}"
fi

expected_hooks_dir="$(cd -- "${hooks_dir}" && pwd -P)"
effective_hooks_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-path hooks)" || {
    echo "install-pre-commit-hook: installed hook but could not resolve Git's effective hooks directory" >&2
    exit 1
}
if [[ "${effective_hooks_dir}" != "${expected_hooks_dir}" ]]; then
    echo "install-pre-commit-hook: installed guard is inactive because core.hooksPath still overrides Git's private hooks directory" >&2
    echo "  expected: ${expected_hooks_dir}" >&2
    echo "  effective: ${effective_hooks_dir}" >&2
    echo "Configured value and origin:" >&2
    git -C "${repo_root}" config --show-origin --get-all core.hooksPath >&2 || true
    echo "Remove the setting from the reported scope, then rerun this installer." >&2
    exit 1
fi

echo "Installed reviewed pre-commit guard at ${entrypoint}"
echo "core.hooksPath is unset; Git will use its private hooks directory."
