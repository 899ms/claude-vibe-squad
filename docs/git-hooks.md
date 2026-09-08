# Git hooks

Vibe Squad installs a reviewed snapshot of its private-memory leak guard in
Git's private hooks directory. The installed executable is outside the mutable
worktree, so checking out another branch cannot replace code that Git runs on
your next commit.

## Install it (one-time, per clone)

From the repository root:

```sh
bash docs/install/install-pre-commit-hook.sh
```

The installer copies [`scripts/hooks/pre-commit`](../scripts/hooks/pre-commit)
to the Git common directory and creates a small managed entrypoint beside it.
Both are regular executable files, not symlinks. Re-run the installer after
pulling an intentional leak-guard update so the reviewed snapshot advances.

Do not point `core.hooksPath` at a tracked directory, and do not link a file in
`.git/hooks/` back into the worktree. Both patterns let the checked-out branch
replace code that runs with the developer's privileges. The tracked
`.githooks/pre-commit` file is not an installation target.

## Verify it

```sh
bash bin/doctor.sh --check-pre-commit-hook
git config --show-origin --get-all core.hooksPath  # expect no output and exit 1
git hook run pre-commit                            # expect no output and exit 0 on a clean index
```

The focused doctor check resolves Git's effective hooks directory rather than
looking only at local config. It fails if a global or system `core.hooksPath`
still overrides the private directory, or if the entrypoint or copied guard is
missing, non-executable, or a symlink. The installer reports the origin of an
effective override and refuses to claim success until it is removed.

## What the local guard does

The installed `vibe-squad-pre-commit` snapshot rejects staged private-memory
artifacts:

- restricted-sensitivity frontmatter;
- paths beneath `_state/bounty/`;
- a literal `${CHRONO_VAULT_ROOT}` phantom path; and
- legacy `kg.db*`, `.db-wal`, and `.db-shm` artifacts.

It reads staged index blobs, so unstaged worktree content cannot hide a staged
restricted version or create a false rejection. Any failure to enumerate or
read the staged objects blocks the commit rather than passing silently.

The installer will not overwrite an unrelated existing pre-commit hook. Review
and compose that hook manually, remove it only when you have decided it is safe
to do so, and rerun the installer. `bin/doctor.sh` treats the unresolved state
as an issue instead of reporting the clone healthy.

## Repository-wide checks

The local snapshot deliberately executes no worktree code. Broader specialist,
capability, format, and moat Tier-A checks therefore run in public CI, where a
contributor branch cannot replace a developer's local executable hook. The
private exact-target Tier-B scanner belongs in private pre-push/CI enforcement;
see [`moat/boundary/README.md`](../moat/boundary/README.md).

The retired Spec-1.5 auto-snapshot check is intentionally absent: current
dispatch leaves Git untouched, so there is no snapshot to require.
