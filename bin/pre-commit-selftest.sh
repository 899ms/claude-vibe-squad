#!/usr/bin/env bash
# Hermetic installed-snapshot controls, plus legacy tracked-hook parity checks.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOOK_TEST_PYTHON="${PRE_COMMIT_PYTHON:-python3}"
# Pin the interpreter for nested legacy wrappers too. Set PRE_COMMIT_PYTHON to
# an absolute virtualenv interpreter when the host has several Python versions.
HOOK_TEST_PYTHON="$(command -v "${HOOK_TEST_PYTHON}")"
export PATH="$(dirname "${HOOK_TEST_PYTHON}"):${PATH}"
export PYTHONDONTWRITEBYTECODE=1
export HOOK_TEST_PYTHON
# The tracked fixture prepends its own PATH. An exported Bash function keeps
# its nested Python calls pinned without editing that read-only hook.
python3() { "${HOOK_TEST_PYTHON}" -B "$@"; }
export -f python3
while IFS= read -r git_variable; do
    unset "${git_variable}"
done < <(git rev-parse --local-env-vars)
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
"${HOOK_TEST_PYTHON}" -I -B "${ROOT}/tests/hooks/test_pre_commit.py" -v
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/vibe-pre-commit.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

TEST_REPO="$TMP_ROOT/repo"
CALL_LOG="$TMP_ROOT/capability-calls.log"
SPECIALIST_CALL_LOG="$TMP_ROOT/specialist-calls.log"

mkdir -p \
    "$TEST_REPO/.githooks" \
    "$TEST_REPO/bin" \
    "$TEST_REPO/scripts/hooks" \
    "$TEST_REPO/scripts/python" \
    "$TEST_REPO/shared/capabilities/project" \
    "$TEST_REPO/shared/registries"
cp "$ROOT/.githooks/pre-commit" "$TEST_REPO/.githooks/pre-commit"
cp "$ROOT/scripts/hooks/pre-commit" "$TEST_REPO/scripts/hooks/pre-commit"
cp "$ROOT/scripts/python/validate_release_version.py" "$TEST_REPO/scripts/python/validate_release_version.py"

cat > "$TEST_REPO/bin/validate-capabilities.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${CAPABILITY_CALL_LOG:?}"
if [[ -n "${CAPABILITY_STUB_FAIL:-}" && "${CAPABILITY_STUB_FAIL}" == "$*" ]]; then
    exit 1
fi
STUB

cat > "$TEST_REPO/bin/validate-specialists.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s|ci=%s\n' "$*" "${SQUAD_CI_HOST_INDEPENDENT:-unset}" \
    >> "${SPECIALIST_CALL_LOG:?}"
if [[ "${SQUAD_CI_HOST_INDEPENDENT:-0}" == "1" ]]; then
    echo "host-independent mode leaked into the local full gate" >&2
    exit 98
fi
if [[ "${SPECIALIST_STUB_FAIL:-0}" == "1" ]]; then
    exit 1
fi
STUB

chmod +x \
    "$TEST_REPO/.githooks/pre-commit" \
    "$TEST_REPO/bin/validate-capabilities.sh" \
    "$TEST_REPO/bin/validate-specialists.sh"

git -C "$TEST_REPO" init -q
git -C "$TEST_REPO" config user.name "Pre-commit Self-test"
git -C "$TEST_REPO" config user.email "pre-commit-selftest@example.invalid"
git -C "$TEST_REPO" config core.hooksPath .githooks
# Keep the real release claims in the baseline so reset retains the gate inputs.
for release_document in CHANGELOG.md CLAUDE.md README.md SECURITY.md; do
    cp "$ROOT/$release_document" "$TEST_REPO/$release_document"
done
git -C "$TEST_REPO" add .
git -C "$TEST_REPO" -c core.hooksPath=/dev/null commit -qm baseline

# A clean index must skip the path-scoped capability pair but still run the full
# specialist/capability-home gate, which is the live-existence enforcement point.
(
    cd "$TEST_REPO"
    CAPABILITY_CALL_LOG="$CALL_LOG" SPECIALIST_CALL_LOG="$SPECIALIST_CALL_LOG" \
        .githooks/pre-commit
)
if [[ -e "$CALL_LOG" ]]; then
    echo "FAIL: clean index invoked the capability validator" >&2
    exit 1
fi
if [[ "$(cat "$SPECIALIST_CALL_LOG")" != "--quiet|ci=unset" ]]; then
    echo "FAIL: clean index did not run the full specialist gate exactly once" >&2
    exit 1
fi

# A staged capability change must run the validator and its self-test exactly.
printf '%s\n' '# staged capability fixture' \
    > "$TEST_REPO/shared/capabilities/project/selftest.md"
git -C "$TEST_REPO" add shared/capabilities/project/selftest.md
(
    cd "$TEST_REPO"
    : > "$SPECIALIST_CALL_LOG"
    CAPABILITY_CALL_LOG="$CALL_LOG" SPECIALIST_CALL_LOG="$SPECIALIST_CALL_LOG" \
        .githooks/pre-commit
)

EXPECTED_CALLS="$TMP_ROOT/expected-calls.log"
printf '\n--self-test\n' > "$EXPECTED_CALLS"
if ! cmp -s "$EXPECTED_CALLS" "$CALL_LOG"; then
    echo "FAIL: staged capability change did not run both expected checks" >&2
    diff -u "$EXPECTED_CALLS" "$CALL_LOG" >&2 || true
    exit 1
fi
if [[ "$(cat "$SPECIALIST_CALL_LOG")" != "--quiet|ci=unset" ]]; then
    echo "FAIL: staged capability change did not run the full specialist gate" >&2
    exit 1
fi

# Either validator failure must block the commit path.
: > "$CALL_LOG"
if (
    cd "$TEST_REPO"
    CAPABILITY_CALL_LOG="$CALL_LOG" CAPABILITY_STUB_FAIL="--self-test" \
        SPECIALIST_CALL_LOG="$SPECIALIST_CALL_LOG" .githooks/pre-commit
); then
    echo "FAIL: capability self-test failure did not block the hook" >&2
    exit 1
fi

# Even a hostile inherited CI-mode flag must be stripped before the local full
# gate.
git -C "$TEST_REPO" reset -q
: > "$SPECIALIST_CALL_LOG"
(
    cd "$TEST_REPO"
    CAPABILITY_CALL_LOG="$CALL_LOG" SPECIALIST_CALL_LOG="$SPECIALIST_CALL_LOG" \
        SQUAD_CI_HOST_INDEPENDENT=1 .githooks/pre-commit
)
if [[ "$(cat "$SPECIALIST_CALL_LOG")" != "--quiet|ci=unset" ]]; then
    echo "FAIL: inherited CI mode was not stripped from the local full gate" >&2
    exit 1
fi

# A live-existence/specialist failure must block a clean index.
if (
    cd "$TEST_REPO"
    CAPABILITY_CALL_LOG="$CALL_LOG" SPECIALIST_CALL_LOG="$SPECIALIST_CALL_LOG" \
        SPECIALIST_STUB_FAIL=1 SQUAD_CI_HOST_INDEPENDENT=1 .githooks/pre-commit
); then
    echo "FAIL: full specialist/live capability failure did not block the hook" >&2
    exit 1
fi

echo "PASS: the full specialist/live capability gate runs on every commit and blocks failures; staged capability changes also run both focused checks"
