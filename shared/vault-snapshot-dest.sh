#!/usr/bin/env bash
# One home for the snapshot producer, nightly report, and retention reaper.
# Source for vault_snapshot_dest, or invoke with bash to print the destination.
# Empty VAULT_SNAPSHOT_DEST has the same meaning as an unset override.
vault_snapshot_dest() {
    printf '%s\n' "${VAULT_SNAPSHOT_DEST:-${HOME:?HOME must be set}/vault-snapshots}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    vault_snapshot_dest
fi
