# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in HYDRA, please report it **privately**:

1. **Preferred:** Use [GitHub Security Advisories](https://github.com/eternal-roman/hydra/security/advisories/new) (private report).
2. **Do NOT** open a public GitHub Issue for exploitable bugs (auth, secret leakage, order injection, RCE via WSL/CLI).
3. **Do NOT** open pull requests containing exploit details or proof-of-concept attack code.

We will acknowledge private reports within 48 hours and work to resolve confirmed vulnerabilities promptly.

## Secrets and API Keys

This project connects to Kraken, Anthropic, and xAI APIs. API keys are loaded from a `.env` file that is **gitignored** and must never be committed. See `.env.example` for the expected keys (placeholders only).

If you fork this repo:
- Never commit `.env`, API keys, JWT secrets, or credential files
- Enable [GitHub Secret Scanning](https://docs.github.com/en/code-security/secret-scanning) and **Push Protection** on your fork
- Rotate any key you suspect has been exposed

Runtime files that must stay local (already gitignored): `hydra_auth_state.json`, `hydra_ws_token.json`, `hydra_users.db`, order journals, session snapshots.

`hydra_ws_token.json` is a live credential: it is the per-process token that unlocks the dashboard WebSocket in both directions. Treat leaking it as leaking read access to the whole account state.

## Built-in boundaries

| Boundary | Where | Behavior |
|---|---|---|
| Dashboard WS | `hydra_ws_server.py` | Binds `127.0.0.1` (`HYDRA_WS_HOST`). Sockets sit in `pending` and receive **no** account state until they authenticate; unauthenticated sockets close after `HYDRA_WS_AUTH_GRACE_S` (default 10s). Inbound commands need the same credential. `Origin` checking is CSRF defence only and fails open for non-browser clients — it is not the auth boundary. |
| Kraken credentials | `KrakenCLI.forward_credentials` | Passed to WSL through the environment via `WSLENV`, never interpolated into `wsl` argv (which is world-readable via `ps`/procfs). Covers the REST wrapper and the long-lived stream subprocesses. `HYDRA_CLI_LEGACY_SECRET_EXPORT=1` reverts, and should not be used on a shared host. |
| CLI argument injection | `KrakenCLI._build_invocation` | Every argument is `shlex.quote`d before reaching `bash -c`. |
| Order surface | engine + agent | Spot, limit, post-only only. No withdraw scope is used or required. |

## Scope

In scope:
- Secret leakage (API keys, credentials, JWT material)
- Command injection via WSL/Kraken CLI calls
- WebSocket vulnerabilities in the dashboard connection
- Logic flaws that could cause unintended order execution

Out of scope:
- Trading strategy effectiveness or financial losses (see README / CHANGELOG: expectancy is research, not a guarantee)
- Issues in third-party dependencies (report upstream)
