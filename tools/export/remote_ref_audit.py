"""Fail-closed audit of a remote's advertised refs against a clean baseline.

A public export can be pristine on ``main`` yet still leak history through a
retained, disjoint ref. GitHub keeps ``refs/pull/N/head`` across a force-push /
clean-slate, so an export projector that only pushes ``main`` can leave the old
lineage publicly fetchable. This audit enumerates every advertised ref and flags
any whose commit is neither reachable from the clean baseline ref nor a new
contribution based on published clean history.

The direction of the history check matters. A ref that is an ancestor of the
baseline is already published history (``clean-ancestor``). New work is a
``contribution`` when it has a merge-base with the baseline and every root
reachable from the ref is also reachable from the baseline. The root-set check
is the whole retained-lineage guard: a merge-base is a common ancestor by
definition, so rechecking whether it is an ancestor of the baseline would add
no condition. A ref with no merge-base or an additional root is a ``LEAK``.
Fork ownership is deliberately irrelevant: a stale fork can reintroduce
retained history just as readily as a same-repository ref.

The root-set check is complete for this repository's retained-history threat
because its clean slate was an orphan rewrite, which minted a new root: reaching
any pre-clean-slate commit necessarily reaches the old root. If a future rewrite
uses ``filter-repo`` or ``filter-branch`` while preserving the root, root-set
equality will no longer prove that old history is absent and this detector must
be replaced or strengthened before that rewrite is certified.

That sufficiency also requires published history to be append-only. A populated,
readable export ledger is therefore a required precondition: the audit refuses
with exit 2 when it is absent, empty, dangling, unreadable, or has no valid
public tip, and also unless that tip is an ancestor of the current baseline.
This prevents a root-preserving rewrite from purging content from the baseline
while a same-root fork still carries it. The precondition is enforced for
publication by ``bin/product-hygiene.sh``, which passes the private checkout's
ledger explicitly; this standalone audit does not authorize a first publication.

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
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


# `projector.DEFAULT_LEDGER_PATH` is authoritative. This standalone module keeps
# the literal locally; the export tests pin the two values together.
DEFAULT_LEDGER_PATH = Path("_state/public-export-2026-07-21/export-ledger.jsonl")


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


def _previous_public_tip(repo: Path, ledger_path: str | Path) -> str:
    ledger = Path(ledger_path)
    if not ledger.is_absolute():
        ledger = repo / ledger
    if ledger.is_symlink() and not ledger.exists():
        raise RemoteRefAuditError(f"export ledger {ledger} is a dangling symlink")
    if not ledger.exists():
        raise RemoteRefAuditError(f"export ledger {ledger} is absent")
    try:
        lines = [
            line
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not lines:
            raise RemoteRefAuditError(f"export ledger {ledger} is empty")
        for line in reversed(lines):
            entry = json.loads(line)
            if not isinstance(entry, dict):
                continue
            tip = entry.get("public_tip") or entry.get("published_tip")
            if tip is None:
                continue
            if not isinstance(tip, str) or not re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tip
            ):
                raise RemoteRefAuditError(
                    f"export ledger {ledger} has an invalid public tip"
                )
            return tip.lower()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RemoteRefAuditError(
            f"cannot read export ledger {ledger}: {error}"
        ) from error
    raise RemoteRefAuditError(f"export ledger {ledger} has no recorded public tip")


def audit_refs(
    repo_dir,
    remote: str,
    clean_ref: str,
    accepted_refs: Mapping[str, str] | None = None,
    *,
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
) -> list[dict]:
    """Return one record per advertised ref on ``remote``.

    Each record includes ``{"ref", "sha", "status"}`` where status is one of
    ``clean-equal`` (== baseline), ``clean-ancestor`` (reachable from baseline),
    ``contribution`` (same-root new commits with a merge-base),
    ``accepted-risk`` (a SHA-pinned, classified exposure), ``LEAK`` (disjoint,
    or carrying an extra root), or ``UNKNOWN``
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
    public_tip = _previous_public_tip(repo, ledger_path)
    if public_tip is not None:
        append_only = _git_result(
            repo, "merge-base", "--is-ancestor", public_tip, clean_sha
        )
        if append_only.returncode == 1:
            raise RemoteRefAuditError(
                "published history is not append-only: export ledger public tip "
                f"{public_tip} is not an ancestor of baseline {clean_sha}"
            )
        if append_only.returncode != 0:
            raise RemoteRefAuditError(
                "cannot verify append-only publication: git merge-base --is-ancestor "
                f"{public_tip} {clean_sha} exited {append_only.returncode}: "
                f"{_result_detail(append_only)}"
            )
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
                    status = "contribution" if ref_roots <= baseline_roots else "LEAK"
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
        "--ledger",
        default=str(DEFAULT_LEDGER_PATH),
        help=f"default: <repo>/{DEFAULT_LEDGER_PATH}",
    )
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
        records = audit_refs(
            args.repo,
            args.remote,
            args.clean_ref,
            accepted_refs,
            ledger_path=args.ledger,
        )
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
