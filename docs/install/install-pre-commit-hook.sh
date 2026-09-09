#!/bin/bash
# Install every reviewed pre-commit guard outside the mutable worktree.
# Unmanaged hooks still refuse by default. After reviewing the existing bytes:
#   bash docs/install/install-pre-commit-hook.sh --replace-unmanaged-sha256 <sha256>
# This explicit identity authorizes replacement, with a retained regular-file
# backup. It never composes or executes the old hook. No --force escape hatch.

set -euo pipefail

replacement_sha256=""
if [[ $# -gt 0 ]]; then
    if [[ $# -ne 2 || "$1" != "--replace-unmanaged-sha256" || ! "$2" =~ ^[0-9a-f]{64}$ ]]; then
        echo "usage: install-pre-commit-hook.sh [--replace-unmanaged-sha256 <reviewed-sha256>]" >&2
        exit 2
    fi
    replacement_sha256="$2"
fi
python_bin="${PRE_COMMIT_PYTHON:-python3}"
hook_sha256() {
    "${python_bin}" -I -B -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "install-pre-commit-hook: run this inside a Git checkout" >&2
    exit 1
}
source_guard="${repo_root}/scripts/hooks/pre-commit"
source_validator="${repo_root}/scripts/python/validate_release_version.py"
if [[ ! -f "${source_guard}" ]]; then
    echo "install-pre-commit-hook: missing reviewed guard: ${source_guard}" >&2
    exit 1
fi
if [[ ! -f "${source_validator}" ]]; then
    echo "install-pre-commit-hook: missing reviewed release validator: ${source_validator}" >&2
    exit 1
fi

git_common_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-common-dir)"
hooks_dir="${git_common_dir}/hooks"
entrypoint="${hooks_dir}/pre-commit"
installed_guard="${hooks_dir}/vibe-squad-pre-commit"
installed_validator="${hooks_dir}/vibe-squad-validate-release-version.py"
installed_pointer="${hooks_dir}/vibe-squad-pre-commit-snapshot.json"
for managed_destination in "${installed_guard}" "${installed_validator}" "${installed_pointer}"; do
    if [[ -L "${managed_destination}" || ( -e "${managed_destination}" && ! -f "${managed_destination}" ) ]]; then
        echo "install-pre-commit-hook: refusing non-regular managed destination: ${managed_destination}" >&2
        exit 1
    fi
done
expected_entrypoint='#!/bin/sh
# vibe-squad-managed-pre-commit/v1
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec python3 "${hook_dir}/vibe-squad-pre-commit"'
previous_sha256=""
backup_required=false

if [[ -L "${hooks_dir}" || -L "${entrypoint}" || ( -e "${entrypoint}" && ! -f "${entrypoint}" ) ]]; then
    echo "install-pre-commit-hook: refusing symlink or non-regular hooks directory/entrypoint: ${entrypoint}" >&2
    exit 1
fi
if [[ -f "${entrypoint}" ]]; then
    previous_sha256="$(hook_sha256 "${entrypoint}")"
    if [[ "$(cat "${entrypoint}")" != "${expected_entrypoint}" ]]; then
        if [[ -z "${replacement_sha256}" || "${replacement_sha256}" != "${previous_sha256}" ]]; then
            echo "install-pre-commit-hook: refusing to replace existing hook: ${entrypoint}" >&2
            echo "Review its bytes, then supply --replace-unmanaged-sha256 with their exact SHA-256; a backup will be retained." >&2
            exit 1
        fi
        backup_required=true
    elif [[ -n "${replacement_sha256}" ]]; then
        echo "install-pre-commit-hook: replacement identity supplied for an already managed hook; rerun without it" >&2
        exit 1
    fi
elif [[ -n "${replacement_sha256}" ]]; then
    echo "install-pre-commit-hook: no existing unmanaged hook matches the supplied identity" >&2
    exit 1
fi
mkdir -p "${hooks_dir}"

guard_tmp="$(mktemp "${hooks_dir}/.vibe-squad-pre-commit.XXXXXX")"
entrypoint_tmp="$(mktemp "${hooks_dir}/.pre-commit.XXXXXX")"
validator_tmp="$(mktemp "${hooks_dir}/.vibe-squad-release-version.XXXXXX")"
pointer_tmp="$(mktemp "${hooks_dir}/.vibe-squad-snapshot-pointer.XXXXXX")"
snapshot_dir="$(mktemp -d "${hooks_dir}/.vibe-squad-snapshot.XXXXXX")"
cleanup() {
    install_status=$?
    rm -f "${guard_tmp}" "${entrypoint_tmp}" "${validator_tmp}" "${pointer_tmp}"
    # Keep a completed bundle on interrupted installs: a published pointer may
    # already refer to it. Old bundles/backups are operator-owned rollback data.
    if [[ "${install_status}" -ne 0 ]]; then
        echo "install-pre-commit-hook: retained snapshot preparation for inspection: ${snapshot_dir}" >&2
    fi
}
trap cleanup EXIT

for reviewed_source in "${source_guard}" "${source_validator}"; do
    if [[ -L "${reviewed_source}" ]]; then
        echo "install-pre-commit-hook: refusing symlink source: ${reviewed_source}" >&2
        exit 1
    fi
done
"${python_bin}" -I -B "${source_guard}" --build-snapshot "${repo_root}" "${snapshot_dir}"
install -m 0644 "${snapshot_dir}/manifest.json" "${pointer_tmp}"
install -m 0755 "${source_guard}" "${guard_tmp}"
install -m 0644 "${source_validator}" "${validator_tmp}"
cat > "${entrypoint_tmp}" <<'HOOK'
#!/bin/sh
# vibe-squad-managed-pre-commit/v1
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec python3 "${hook_dir}/vibe-squad-pre-commit"
HOOK
chmod 0755 "${entrypoint_tmp}"

# Recheck the reviewed identity immediately before publishing anything. Refuse
# a concurrent edit, including an entrypoint that appeared during preparation.
if [[ -n "${previous_sha256}" ]]; then
    if [[ -L "${entrypoint}" || ! -f "${entrypoint}" || "$(hook_sha256 "${entrypoint}")" != "${previous_sha256}" ]]; then
        echo "install-pre-commit-hook: existing hook changed during installation; refusing replacement" >&2
        exit 1
    fi
elif [[ -e "${entrypoint}" || -L "${entrypoint}" ]]; then
    echo "install-pre-commit-hook: a hook appeared during installation; refusing replacement" >&2
    exit 1
fi
if $backup_required; then
    backup_path="$(mktemp "${hooks_dir}/pre-commit.unmanaged.${previous_sha256}.XXXXXX")"
    install -m 0755 "${entrypoint}" "${backup_path}"
    if [[ "$(hook_sha256 "${backup_path}")" != "${previous_sha256}" ]]; then
        echo "install-pre-commit-hook: backup identity changed; refusing replacement" >&2
        exit 1
    fi
    echo "Preserved reviewed unmanaged hook at ${backup_path}"
fi
# Flush the complete bundle and new files before the entrypoint is published.
"${python_bin}" -I -B - "${snapshot_dir}" "${guard_tmp}" "${validator_tmp}" "${pointer_tmp}" "${entrypoint_tmp}" <<'PY'
import os
from pathlib import Path
import sys
paths = list(Path(sys.argv[1]).rglob("*")) + [Path(path) for path in sys.argv[2:]]
for path in paths:
    if path.is_file():
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
PY
mv -f "${pointer_tmp}" "${installed_pointer}"
mv -f "${validator_tmp}" "${installed_validator}"
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
echo "Installed reviewed release-version validator at ${installed_validator}"
echo "Installed reviewed validator/dependency bundle at ${snapshot_dir}"
echo "core.hooksPath is unset; Git will use its private hooks directory."
