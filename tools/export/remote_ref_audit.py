"""Fail-closed audit of a remote's advertised refs against a clean baseline.

A public export can be pristine on ``main`` yet still leak history through a
retained, disjoint ref. GitHub keeps ``refs/pull/N/head`` across a force-push /
clean-slate, so an export projector that only pushes ``main`` can leave the old
lineage publicly fetchable. This audit enumerates every advertised ref and flags
any whose commit is neither reachable from the clean baseline ref nor a new
contribution based exactly on that baseline.

The direction of the history check matters. A ref that is an ancestor of the
baseline is already published history (``clean-ancestor``), while a ref whose
merge-base is the baseline is new work built on the clean slate only when every
root reachable from the ref is also reachable from the baseline
(``contribution``). A ref that diverges before the baseline, has no merge-base,
or reaches an additional root still carries lineage outside the clean slate and
is a ``LEAK``. Fork ownership is deliberately irrelevant: a stale fork can
reintroduce retained history just as readily as a same-repository ref.

The root-set check is complete for this repository's retained-history threat
because its clean slate was an orphan rewrite, which minted a new root: reaching
any pre-clean-slate commit necessarily reaches the old root. If a future rewrite
uses ``filter-repo`` or ``filter-branch`` while preserving the root, root-set
equality will no longer prove that old history is absent and this detector must
be replaced or strengthened before that rewrite is certified.

This root-set check bounds lineage, not content. Replaying old commits onto the
new root mints a new lineage and therefore classifies as ``contribution`` by
design, even when its trees reproduce pre-clean-slate content. Closing that gap
requires content scanning of every advertised ref; no gate in the current chain
does so, because gitleaks and the entropy scan inspect only the tracked working
tree, never advertised refs.

Every advertised ref is fetched explicitly before it is classified; merely
seeing its object ID in ``ls-remote`` is not evidence that the object exists
locally. If initial remote access fails, the audit cannot establish its evidence
set and exits 2. If enumeration succeeds but an individual object cannot be
fetched or evaluated (including a later network failure), that ref is
``UNKNOWN`` and the completed audit fails closed with exit 1.

Standalone module (``tools/export`` is not a package): run as
``python3 tools/export/remote_ref_audit.py --remote public``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


class RemoteRefAuditError(RuntimeError):
    """The advertised-ref audit could not obtain trustworthy evidence."""


def _git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no diagnostic output").strip()
        operation = " ".join(args)
        raise RemoteRefAuditError(
            f"git {operation} exited {error.returncode}: {detail}"
        ) from None


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git without converting a meaningful nonzero result into an error."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "no diagnostic output").strip()


def audit_refs(
    repo_dir,
    remote: str,
    clean_ref: str,
    accepted_refs: Mapping[str, str] | None = None,
) -> list[dict]:
    """Return one record per advertised ref on ``remote``.

    Each record includes ``{"ref", "sha", "status"}`` where status is one of
    ``clean-equal`` (== baseline), ``clean-ancestor`` (reachable from baseline),
    ``contribution`` (the baseline plus same-root new commits),
    ``accepted-risk`` (a SHA-pinned, classified exposure), ``LEAK`` (disjoint,
    diverged before the baseline, or carrying an extra root), or ``UNKNOWN``
    (the advertised object could not be fetched or evaluated). ``accepted-risk``
    records include the underlying ``classification``; ``UNKNOWN`` records
    include a diagnostic ``detail``. A SHA-matching acceptance on any status
    other than ``LEAK`` leaves that status unchanged and adds
    ``stale_acceptance=True``.
    """
    repo = Path(repo_dir)
    accepted_refs = accepted_refs or {}
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip().lower()
    if shallow != "false":
        if shallow == "true":
            raise RemoteRefAuditError(
                "repository is shallow; .git/shallow prevents establishing true roots"
            )
        raise RemoteRefAuditError(
            "git rev-parse --is-shallow-repository returned "
            f"an unexpected value: {shallow or '<empty>'}"
        )
    _git(repo, "fetch", "--quiet", remote)
    clean_sha = _git(repo, "rev-parse", clean_ref).strip()
    baseline_roots = {
        line.strip()
        for line in _git(repo, "rev-list", "--max-parents=0", clean_sha).splitlines()
        if line.strip()
    }
    if not baseline_roots:
        raise RemoteRefAuditError(f"baseline {clean_sha} has no reachable root commit")
    records: list[dict] = []
    for line in _git(repo, "ls-remote", remote).splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if not ref or ref == "HEAD" or ref.endswith("^{}"):
            continue

        try:
            _git(repo, "fetch", "--quiet", "--no-tags", remote, ref)
            _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        except RemoteRefAuditError as error:
            records.append(
                {"ref": ref, "sha": sha, "status": "UNKNOWN", "detail": str(error)}
            )
            continue

        if sha == clean_sha:
            status = "clean-equal"
        else:
            ancestor = _git_result(repo, "merge-base", "--is-ancestor", sha, clean_sha)
            if ancestor.returncode == 0:
                status = "clean-ancestor"
            elif ancestor.returncode != 1:
                records.append(
                    {
                        "ref": ref,
                        "sha": sha,
                        "status": "UNKNOWN",
                        "detail": (
                            "git merge-base --is-ancestor "
                            f"{sha} {clean_sha} exited {ancestor.returncode}: "
                            f"{_result_detail(ancestor)}"
                        ),
                    }
                )
                continue
            else:
                merge_base = _git_result(repo, "merge-base", sha, clean_sha)
                if merge_base.returncode == 0:
                    if merge_base.stdout.strip() != clean_sha:
                        status = "LEAK"
                    else:
                        roots = _git_result(repo, "rev-list", "--max-parents=0", sha)
                        ref_roots = {
                            line.strip()
                            for line in roots.stdout.splitlines()
                            if line.strip()
                        }
                        if roots.returncode != 0 or not ref_roots:
                            records.append(
                                {
                                    "ref": ref,
                                    "sha": sha,
                                    "status": "UNKNOWN",
                                    "detail": (
                                        "git rev-list --max-parents=0 "
                                        f"{sha} exited {roots.returncode}: "
                                        f"{_result_detail(roots)}"
                                    ),
                                }
                            )
                            continue
                        status = (
                            "contribution" if ref_roots <= baseline_roots else "LEAK"
                        )
                elif merge_base.returncode == 1:
                    # No merge-base is the original retained/disjoint leak shape.
                    status = "LEAK"
                else:
                    records.append(
                        {
                            "ref": ref,
                            "sha": sha,
                            "status": "UNKNOWN",
                            "detail": (
                                f"git merge-base {sha} {clean_sha} exited "
                                f"{merge_base.returncode}: {_result_detail(merge_base)}"
                            ),
                        }
                    )
                    continue
        records.append({"ref": ref, "sha": sha, "status": status})
    if not records:
        raise RemoteRefAuditError("remote advertised no evaluable refs")
    for record in records:
        accepted_sha = accepted_refs.get(record["ref"], "").lower()
        if not accepted_sha or record["sha"].lower() != accepted_sha:
            continue
        if record["status"] == "LEAK":
            record["status"] = "accepted-risk"
            record["classification"] = "LEAK"
        else:
            record["stale_acceptance"] = True
    return records


def _parse_accepted_refs(
    values: list[str], parser: argparse.ArgumentParser
) -> dict[str, str]:
    accepted: dict[str, str] = {}
    for value in values:
        ref, separator, sha = value.rpartition("=")
        valid_sha = re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha)
        if not separator or not ref or not valid_sha:
            parser.error(
                "--accept-ref must be REF=FULL_SHA (40 or 64 hexadecimal characters)"
            )
        normalized = sha.lower()
        if ref in accepted and accepted[ref] != normalized:
            parser.error(f"--accept-ref gives conflicting SHAs for {ref}")
        accepted[ref] = normalized
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="public")
    parser.add_argument("--clean-ref", default="refs/remotes/public/main")
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--accept-ref",
        action="append",
        default=[],
        metavar="REF=FULL_SHA",
        help="accept one classified exposure only while the ref matches this SHA",
    )
    args = parser.parse_args()
    accepted_refs = _parse_accepted_refs(args.accept_ref, parser)

    try:
        records = audit_refs(args.repo, args.remote, args.clean_ref, accepted_refs)
    except RemoteRefAuditError as error:
        print(f"ERROR: remote-ref audit could not complete: {error}", file=sys.stderr)
        return 2
    leaks = [r for r in records if r["status"] == "LEAK"]
    unknown = [r for r in records if r["status"] == "UNKNOWN"]
    advertised_refs = {record["ref"] for record in records}
    for accepted_ref in sorted(accepted_refs.keys() - advertised_refs):
        print(
            f"STALE ACCEPTANCE: {accepted_ref}: ref was not advertised",
            file=sys.stderr,
        )
    for record in records:
        suffix = (
            f" (suppressed {record['classification']})"
            if record["status"] == "accepted-risk"
            else ""
        )
        print(f"{record['sha']}  {record['ref']}  {record['status']}{suffix}")
        if record.get("stale_acceptance"):
            print(
                f"STALE ACCEPTANCE: {record['ref']}: underlying classification is "
                f"{record['status']}",
                file=sys.stderr,
            )
    if leaks or unknown:
        for record in unknown:
            print(
                f"UNKNOWN: {record['ref']}: {record['detail']}",
                file=sys.stderr,
            )
        print(
            f"FAIL: {len(leaks)} advertised ref(s) carry unexpected lineage; "
            f"{len(unknown)} advertised ref(s) could not be evaluated",
            file=sys.stderr,
        )
        return 1
    print("PASS: every advertised ref is clean vs the baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
