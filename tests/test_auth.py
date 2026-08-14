"""Tests for hydra_auth v2.17.1 hardening.

Covers:
- No admin seeded when HYDRA_ADMIN_PASSWORD is unset.
- Admin seeded only when HYDRA_ADMIN_PASSWORD is set and users table is empty.
- SECRET_KEY / ENCRYPTION_KEY persist across "restarts" via
  hydra_auth_state.json fallback; tokens survive, encrypted payloads survive.
- Env var HYDRA_JWT_SECRET / HYDRA_ENCRYPTION_KEY still win.
- create-user CLI happy path + duplicate rejection.
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _fresh_auth_env(tmp: Path, env_overrides: dict = None):
    """Return an env dict scoped to a clean tmp workspace for hydra_auth."""
    env = {k: v for k, v in os.environ.items()
           if k not in {
               "HYDRA_ADMIN_PASSWORD",
               "HYDRA_JWT_SECRET",
               "HYDRA_ENCRYPTION_KEY",
               "HYDRA_NEW_USER_PASSWORD",
           }}
    env["HYDRA_AUTH_DB_PATH"] = str(tmp / "users.db")
    env["HYDRA_AUTH_STATE_PATH"] = str(tmp / "auth_state.json")
    env["PYTHONPATH"] = str(ROOT)
    if env_overrides:
        env.update(env_overrides)
    return env


def _run_in_subprocess(env, script):
    """Run a Python snippet in a fresh subprocess so hydra_auth re-imports cleanly."""
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


class TestAdminBootstrap(unittest.TestCase):

    def test_no_admin_seeded_without_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp))
            r = _run_in_subprocess(env, (
                "import hydra_auth, sqlite3;"
                "conn = sqlite3.connect(hydra_auth.DB_PATH);"
                "rows = conn.execute('SELECT username FROM users').fetchall();"
                "conn.close();"
                "print('ROWS=', rows)"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ROWS= []", r.stdout)
            self.assertIn("No users in auth DB", r.stderr)

    def test_admin_seeded_only_when_env_var_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_ADMIN_PASSWORD": "rotate-me-please"})
            r = _run_in_subprocess(env, (
                "import hydra_auth;"
                "u = hydra_auth.authenticate_user('admin', 'rotate-me-please');"
                "assert u and u['role'] == 'admin', u;"
                "bad = hydra_auth.authenticate_user('admin', 'admin');"
                "assert bad is None, 'default admin/admin must NOT auth';"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("OK", r.stdout)

    def test_default_admin_password_never_works(self):
        """Regression: the removed v2.17.0 default 'admin'/'admin' must not auth."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp))
            r = _run_in_subprocess(env, (
                "import hydra_auth;"
                "assert hydra_auth.authenticate_user('admin', 'admin') is None;"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_legacy_admin_admin_db_triggers_warning(self):
        """Operators with a pre-v2.17.1 DB containing admin/admin must be warned every startup."""
        with tempfile.TemporaryDirectory() as tmp:
            # Build a "legacy" DB by importing hydra_auth with the old default password set,
            # then re-import without the env var — _audit_legacy_default_admin should fire.
            env_setup = _fresh_auth_env(Path(tmp), {"HYDRA_ADMIN_PASSWORD": "admin"})
            r0 = _run_in_subprocess(env_setup, "import hydra_auth; print('seeded')")
            self.assertEqual(r0.returncode, 0, r0.stderr)
            env_probe = _fresh_auth_env(Path(tmp))
            r1 = _run_in_subprocess(env_probe, "import hydra_auth; print('ok')")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("'admin'/'admin' still authenticates", r1.stderr)


class TestSecretPersistence(unittest.TestCase):

    def test_secrets_persist_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp))
            # First "run": mint a token and encrypt a payload.
            r1 = _run_in_subprocess(env, (
                "import hydra_auth;"
                "tok = hydra_auth.create_access_token({'sub': 'alice'});"
                "enc = hydra_auth.fernet.encrypt(b'sekret').decode();"
                "print('TOK=' + tok);"
                "print('ENC=' + enc)"
            ))
            self.assertEqual(r1.returncode, 0, r1.stderr)
            tok = [l for l in r1.stdout.splitlines() if l.startswith("TOK=")][0][4:]
            enc = [l for l in r1.stdout.splitlines() if l.startswith("ENC=")][0][4:]
            # State file must exist and contain both fields.
            state_path = Path(tmp) / "auth_state.json"
            self.assertTrue(state_path.exists(), "state file not persisted")
            state = json.loads(state_path.read_text())
            self.assertIn("jwt_secret", state)
            self.assertIn("encryption_key", state)
            # Second "run": same file, must decrypt + verify successfully.
            r2 = _run_in_subprocess(env, (
                "import hydra_auth;"
                f"assert hydra_auth.verify_token({tok!r}) is not None, 'token lost';"
                f"assert hydra_auth.fernet.decrypt({enc!r}.encode()).decode() == 'sekret';"
                "print('OK')"
            ))
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("OK", r2.stdout)

    def test_env_var_overrides_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env1 = _fresh_auth_env(Path(tmp))
            # Seed a state file.
            _run_in_subprocess(env1, "import hydra_auth; print(hydra_auth.SECRET_KEY)")
            state = json.loads((Path(tmp) / "auth_state.json").read_text())
            # Now override with an env var.
            env2 = _fresh_auth_env(Path(tmp), {"HYDRA_JWT_SECRET": "envvar-wins-xyz"})
            r = _run_in_subprocess(env2, (
                "import hydra_auth;"
                "assert hydra_auth.SECRET_KEY == 'envvar-wins-xyz', hydra_auth.SECRET_KEY;"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)
            # State file must NOT have been clobbered by the env-var run.
            state_after = json.loads((Path(tmp) / "auth_state.json").read_text())
            self.assertEqual(state["jwt_secret"], state_after["jwt_secret"])

    def test_corrupt_state_file_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "auth_state.json").write_text("not-json{{{")
            env = _fresh_auth_env(Path(tmp))
            r = _run_in_subprocess(env, (
                "import hydra_auth;"
                "tok = hydra_auth.create_access_token({'sub': 'x'});"
                "assert hydra_auth.verify_token(tok) is not None;"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)


class TestCreateUserCli(unittest.TestCase):

    def test_cli_create_user_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "alice"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Created user 'alice'", r.stdout)
            # Follow-up auth in a child process must succeed.
            r2 = _run_in_subprocess(env, (
                "import hydra_auth;"
                "u = hydra_auth.authenticate_user('alice', 's3cretpw');"
                "assert u and u['role'] == 'user';"
                "print('OK')"
            ))
            self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_cli_create_user_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "bob"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "bob"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("already exists", r.stderr)

    def test_cli_create_admin_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "root", "--admin"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Created admin user 'root'", r.stdout)

    def test_cli_duplicate_points_at_reset_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "bob"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "bob"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("reset-password", r.stderr)

    def test_cli_reset_password_rotates_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "old-pass"})
            subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "alice"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            env["HYDRA_NEW_USER_PASSWORD"] = "new-pass"
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "reset-password", "alice"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Reset password for 'alice'", r.stdout)
            r2 = _run_in_subprocess(env, (
                "import hydra_auth;"
                "assert hydra_auth.authenticate_user('alice', 'new-pass');"
                "assert hydra_auth.authenticate_user('alice', 'old-pass') is None;"
                "print('OK')"
            ))
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("OK", r2.stdout)

    def test_cli_reset_password_unknown_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "x"})
            r = subprocess.run(
                [sys.executable, "hydra_auth.py", "reset-password", "nobody"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("not found", r.stderr)

    def test_authenticate_strips_and_ignores_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "eternal-roman"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            r = _run_in_subprocess(env, (
                "import hydra_auth;"
                "u = hydra_auth.authenticate_user(' Eternal-Roman ', 's3cretpw');"
                "assert u and u['username'] == 'eternal-roman', u;"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_login_jwt_sub_is_canonical_username(self):
        """Typed case/whitespace must not leak into JWT sub or key lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _fresh_auth_env(Path(tmp), {"HYDRA_NEW_USER_PASSWORD": "s3cretpw"})
            subprocess.run(
                [sys.executable, "hydra_auth.py", "create-user", "eternal-roman"],
                env=env, capture_output=True, text=True, cwd=str(ROOT),
            )
            r = _run_in_subprocess(env, (
                "import hydra_auth, hydra_ws_server;"
                "b = hydra_ws_server.DashboardBroadcaster.__new__("
                "    hydra_ws_server.DashboardBroadcaster);"
                "ack = hydra_ws_server.DashboardBroadcaster._handle_login("
                "    b, {'username': ' Eternal-Roman ', 'password': 's3cretpw'});"
                "assert ack.get('success'), ack;"
                "payload = hydra_auth.verify_token(ack['token']);"
                "assert payload and payload.get('sub') == 'eternal-roman', payload;"
                "keys = hydra_auth.get_api_keys_by_username('ETERNAL-ROMAN', 'kraken');"
                "assert keys is None;"
                "print('OK')"
            ))
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
