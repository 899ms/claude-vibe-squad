"""bin/daemon-launcher.sh: secrets sourcing must survive an unset reference but never launch with an empty token.

Run directly: .venv/bin/python -m unittest scripts/python/tests/test_daemon_launcher.py
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "bin" / "daemon-launcher.sh"
EXEC_LINE = 'exec "$REPO/.venv/bin/python" -m daemon.main'


class DaemonLauncherSecretsTests(unittest.TestCase):
    def run_launcher(self, secrets: str | None, env_token: str | None = None) -> subprocess.CompletedProcess:
        # Execute a stub in memory; pin the source path because bash -c has no
        # script location. This keeps tests from writing beside the real launcher.
        home = Path(tempfile.mkdtemp(prefix="launcher-home-"))
        if secrets is not None:
            (home / ".config" / "shell").mkdir(parents=True)
            (home / ".config" / "shell" / "secrets.zsh").write_text(secrets)
        text = LAUNCHER.read_text()
        assert EXEC_LINE in text, "launcher exec line moved; update the test stub"
        text = text.replace(EXEC_LINE, 'echo "WOULD_EXEC daemon.main"')
        source_line = next(line for line in text.splitlines() if line.startswith('source "$(cd'))
        text = text.replace(source_line, "source " + shlex.quote(str(ROOT / "shared/repo-root.sh")))
        env = {"HOME": str(home), "PATH": os.environ["PATH"]}
        if env_token is not None:
            env["VIBESQUAD_DAEMON_TOKEN"] = env_token
        return subprocess.run(["bash", "-c", text], env=env, capture_output=True, text=True, timeout=30)

    def test_empty_consumed_paths_are_rejected_after_relaxed_source(self):
        for name in ("VIBE_SQUAD_ROOT", "VIBE_PLUGINS", "VIBE_PYTHON", "VIBESQUAD_STATE_DIR", "VAULT_ROOT"):
            for value in ('""', '"$DEFINITELY_UNSET_PATH"'):
                with self.subTest(name=name, value=value):
                    result = self.run_launcher(f'export VIBESQUAD_DAEMON_TOKEN="tok-123"\nexport {name}={value}\n')
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertNotIn("WOULD_EXEC", result.stdout)
                    self.assertIn(f"{name} is empty", result.stderr)

    def test_nonempty_consumed_paths_remain_accepted(self):
        secrets = 'export VIBESQUAD_DAEMON_TOKEN="tok-123"\n'
        for name in ("VIBE_SQUAD_ROOT", "VIBE_PLUGINS", "VIBE_PYTHON", "VIBESQUAD_STATE_DIR", "VAULT_ROOT"):
            secrets += f"export {name}={shlex.quote(str(ROOT))}\n"
        result = self.run_launcher(secrets)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WOULD_EXEC daemon.main", result.stdout)

    def test_unset_reference_in_secrets_does_not_abort_launch(self):
        result = self.run_launcher('export VIBESQUAD_DAEMON_TOKEN="tok-123"\nexport NANO="$DEFINITELY_UNSET_VAR"\n')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WOULD_EXEC daemon.main", result.stdout)

    def test_unset_reference_in_secrets_is_reported_on_stderr(self):
        result = self.run_launcher('export VIBESQUAD_DAEMON_TOKEN="tok-123"\nexport API_URL="https://$DEFINITELY_UNSET_VAR/path"\n')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEFINITELY_UNSET_VAR", result.stderr)
        self.assertIn("unset", result.stderr.lower())

    def test_clean_secrets_file_produces_no_warning(self):
        result = self.run_launcher('export VIBESQUAD_DAEMON_TOKEN="tok-123"\nexport OTHER="${MAYBE_UNSET:-}"\n')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unset", result.stderr.lower())

    def test_empty_token_after_sourcing_fails_loudly_before_exec(self):
        result = self.run_launcher('export VIBESQUAD_DAEMON_TOKEN="$VIBESQUAD_DAEMON_TOKNE"\n')

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WOULD_EXEC", result.stdout)
        self.assertIn("VIBESQUAD_DAEMON_TOKEN", result.stderr)
        self.assertIn("empty", result.stderr)

    def test_missing_secrets_file_without_env_token_fails_loudly(self):
        result = self.run_launcher(secrets=None)

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WOULD_EXEC", result.stdout)
        self.assertIn("VIBESQUAD_DAEMON_TOKEN", result.stderr)

    def test_missing_secrets_file_with_env_token_launches(self):
        result = self.run_launcher(secrets=None, env_token="tok-from-launchd")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WOULD_EXEC daemon.main", result.stdout)


if __name__ == "__main__":
    unittest.main()
