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

## Scope

In scope:
- Secret leakage (API keys, credentials, JWT material)
- Command injection via WSL/Kraken CLI calls
- WebSocket vulnerabilities in the dashboard connection
- Logic flaws that could cause unintended order execution

Out of scope:
- Trading strategy effectiveness or financial losses
- Issues in third-party dependencies (report upstream)
