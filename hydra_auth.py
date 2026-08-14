"""Hydra Authentication and Session Management.

v2.17.1 hardening:
- `SECRET_KEY` and `ENCRYPTION_KEY` persist across restarts via
  `hydra_auth_state.json` (gitignored, 0600 on POSIX). Env vars
  `HYDRA_JWT_SECRET` / `HYDRA_ENCRYPTION_KEY` still take precedence;
  the state file is a fallback so that a forgotten env var does not
  invalidate all sessions or make previously-encrypted API secrets
  undecryptable on the next process start.
- No hardcoded `admin/admin` default. First-run admin creation requires
  `HYDRA_ADMIN_PASSWORD`; otherwise a bootstrap instruction is printed
  and no user is seeded. For manual provisioning use the CLI:
      python hydra_auth.py create-user <username> [--admin]
      python hydra_auth.py reset-password <username>
"""
import sqlite3
import os
import sys
import time
import json
import getpass
import jwt
import base64
from pathlib import Path
from cryptography.fernet import Fernet
from passlib.context import CryptContext
from typing import Optional, Dict, Tuple

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

DB_PATH = os.environ.get("HYDRA_AUTH_DB_PATH", "hydra_users.db")
AUTH_STATE_PATH = Path(os.environ.get("HYDRA_AUTH_STATE_PATH", "hydra_auth_state.json"))


def _load_state_file() -> dict:
    if not AUTH_STATE_PATH.exists():
        return {}
    try:
        with open(AUTH_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _persist_state_file(state: dict) -> None:
    try:
        tmp = AUTH_STATE_PATH.with_suffix(AUTH_STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, AUTH_STATE_PATH)
        try:
            os.chmod(AUTH_STATE_PATH, 0o600)
        except (OSError, NotImplementedError):
            # Windows ACLs differ; best-effort only.
            pass
    except OSError as e:
        print(f"[AUTH] WARNING: could not persist {AUTH_STATE_PATH}: {e}",
              file=sys.stderr)


def _resolve_secrets() -> Tuple[str, str]:
    """Resolve JWT secret + Fernet encryption key.

    Priority: env var → state file → generate + persist.
    Generation-path emits a single stderr warning telling the operator
    to pin via env var for production.
    """
    state = _load_state_file()
    changed = False

    jwt_secret = os.environ.get("HYDRA_JWT_SECRET") or state.get("jwt_secret")
    if not jwt_secret:
        jwt_secret = os.urandom(32).hex()
        state["jwt_secret"] = jwt_secret
        changed = True
        print(
            f"[AUTH] No HYDRA_JWT_SECRET; generated and persisted to "
            f"{AUTH_STATE_PATH}. Pin HYDRA_JWT_SECRET for production.",
            file=sys.stderr,
        )

    enc_key = os.environ.get("HYDRA_ENCRYPTION_KEY") or state.get("encryption_key")
    if not enc_key:
        enc_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        state["encryption_key"] = enc_key
        changed = True
        print(
            f"[AUTH] No HYDRA_ENCRYPTION_KEY; generated and persisted to "
            f"{AUTH_STATE_PATH}. Pin HYDRA_ENCRYPTION_KEY for production.",
            file=sys.stderr,
        )

    if changed:
        _persist_state_file(state)

    return jwt_secret, enc_key


SECRET_KEY, ENCRYPTION_KEY = _resolve_secrets()
fernet = Fernet(ENCRYPTION_KEY.encode())

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def init_db():
    """Initialize the SQLite database for user credentials.

    Seeds an admin user iff `HYDRA_ADMIN_PASSWORD` is set and the users
    table is empty. Never seeds a default/known password.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at REAL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            user_id INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            api_key TEXT NOT NULL,
            api_secret_encrypted TEXT NOT NULL,
            created_at REAL,
            PRIMARY KEY (user_id, exchange),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        admin_password = os.environ.get("HYDRA_ADMIN_PASSWORD")
        if admin_password:
            admin_hash = pwd_context.hash(admin_password)
            c.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                ("admin", admin_hash, "admin", time.time()),
            )
            print("[AUTH] Seeded admin user from HYDRA_ADMIN_PASSWORD.", file=sys.stderr)
        else:
            print(
                "[AUTH] No users in auth DB. Set HYDRA_ADMIN_PASSWORD and "
                "restart, or run: python hydra_auth.py create-user <username>",
                file=sys.stderr,
            )
    conn.commit()
    conn.close()


def create_user(username: str, password: str, role: str = "user") -> bool:
    """Create a new user. Returns True if successful, False if exists."""
    username = (username or "").strip()
    password_hash = pwd_context.hash(password)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                  (username, password_hash, role, time.time()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def set_password(username: str, password: str) -> bool:
    """Replace the stored hash for an existing user. False if not found."""
    username = (username or "").strip()
    if not username or not password:
        return False
    password_hash = pwd_context.hash(password)
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET password_hash = ? WHERE username = ? COLLATE NOCASE",
            (password_hash, username),
        )
        conn.commit()
        return c.rowcount > 0
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Optional[Dict]:
    """Verify credentials and return user info if valid."""
    username = (username or "").strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, username, password_hash, role FROM users "
        "WHERE username = ? COLLATE NOCASE",
        (username,),
    )
    user = c.fetchone()
    conn.close()

    if not user:
        return None

    user_id, u_name, p_hash, role = user
    if pwd_context.verify(password, p_hash):
        return {"id": user_id, "username": u_name, "role": role}
    return None


def create_access_token(data: dict) -> str:
    """Generate a JWT token for the session."""
    to_encode = data.copy()
    expire = time.time() + (ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[Dict]:
    """Verify a JWT token and return payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


def save_api_keys(user_id: int, exchange: str, api_key: str, api_secret: str) -> bool:
    """Securely encrypt and save API keys for a user/exchange."""
    encrypted_secret = fernet.encrypt(api_secret.encode()).decode()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO api_keys (user_id, exchange, api_key, api_secret_encrypted, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, exchange) DO UPDATE SET
            api_key = excluded.api_key,
            api_secret_encrypted = excluded.api_secret_encrypted,
            created_at = excluded.created_at
        """, (user_id, exchange, api_key, encrypted_secret, time.time()))
        conn.commit()
        return True
    except Exception as e:
        import logging
        logging.error(f"Failed to save API keys: {e}")
        return False
    finally:
        conn.close()


def get_api_keys(user_id: int, exchange: str) -> Optional[Dict[str, str]]:
    """Retrieve and decrypt API keys for a user/exchange."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT api_key, api_secret_encrypted FROM api_keys WHERE user_id = ? AND exchange = ?", (user_id, exchange))
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    api_key, encrypted_secret = row
    try:
        decrypted_secret = fernet.decrypt(encrypted_secret.encode()).decode()
        return {"api_key": api_key, "api_secret": decrypted_secret}
    except Exception as e:
        import logging
        logging.error(f"Failed to decrypt API secret for user {user_id}: {e}")
        return None


def get_api_keys_by_username(username: str, exchange: str) -> Optional[Dict[str, str]]:
    """Retrieve API keys using username instead of user_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
        ((username or "").strip(),),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return get_api_keys(row[0], exchange)


def _read_cli_password(username: str) -> Optional[str]:
    password = os.environ.get("HYDRA_NEW_USER_PASSWORD") or getpass.getpass(
        f"Password for {username}: "
    )
    if not password:
        print("[AUTH] Empty password rejected.", file=sys.stderr)
        return None
    return password


def _cli_create_user(argv):
    if len(argv) < 1:
        print("usage: python hydra_auth.py create-user <username> [--admin]", file=sys.stderr)
        return 2
    username = argv[0].strip()
    role = "admin" if "--admin" in argv[1:] else "user"
    password = _read_cli_password(username)
    if password is None:
        return 2
    if create_user(username, password, role=role):
        label = "admin user" if role == "admin" else "user"
        print(f"[AUTH] Created {label} '{username}'.")
        return 0
    print(
        f"[AUTH] User '{username}' already exists. "
        f"Password was NOT changed. Reset with: "
        f"python hydra_auth.py reset-password {username}",
        file=sys.stderr,
    )
    return 1


def _cli_reset_password(argv):
    if len(argv) < 1:
        print("usage: python hydra_auth.py reset-password <username>", file=sys.stderr)
        return 2
    username = argv[0].strip()
    password = _read_cli_password(username)
    if password is None:
        return 2
    if set_password(username, password):
        print(f"[AUTH] Reset password for '{username}'.")
        return 0
    print(f"[AUTH] User '{username}' not found.", file=sys.stderr)
    return 1


def _audit_legacy_default_admin():
    """Warn operators whose DB was seeded by the pre-v2.17.1 default.

    Pre-v2.17.1, init_db() silently seeded an `admin`/`admin` row whenever
    the users table was empty. An install that ran any earlier release now
    has a known, published admin credential. Detect and warn on every
    startup until rotated.
    """
    try:
        if authenticate_user("admin", "admin") is not None:
            print(
                "[AUTH] WARNING: 'admin'/'admin' still authenticates against "
                f"{DB_PATH}. This is the insecure default removed in v2.17.1. "
                "Rotate now: delete the admin row (e.g. "
                "`sqlite3 hydra_users.db \"DELETE FROM users WHERE username='admin'\"`) "
                "and recreate via `python hydra_auth.py create-user admin --admin`.",
                file=sys.stderr,
            )
    except Exception:
        # DB not yet ready or locked — don't block module import.
        pass


# Initialize db on module load
init_db()
_audit_legacy_default_admin()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create-user":
        sys.exit(_cli_create_user(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "reset-password":
        sys.exit(_cli_reset_password(sys.argv[2:]))
    print(
        "usage: python hydra_auth.py create-user <username> [--admin]\n"
        "       python hydra_auth.py reset-password <username>",
        file=sys.stderr,
    )
    sys.exit(2)
