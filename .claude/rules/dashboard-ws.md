---
paths:
  - "hydra_ws_server.py"
  - "dashboard/src/**"
  - "tests/test_auth.py"
  - "tests/test_ws_token_files.py"
---

# Dashboard WebSocket auth invariant (HIGH severity if violated)

Moved verbatim from `CLAUDE.md` §Pairs, config and auth. Loads when the
files above are touched.

- **The dashboard WS authenticates BOTH directions** — an accepted socket sits in `DashboardBroadcaster.pending` and receives nothing (no `latest_state`, no broadcast) until `_promote_client` moves it into `clients`, reached by a `{"type":"auth"}` handshake or any command whose credential passes `_client_authenticated` (per-process token; JWT in production mode). Unauthenticated sockets close after `HYDRA_WS_AUTH_GRACE_S`. Auth, not loopback binding, protects the account. `_origin_allowed` deliberately fails open on a missing `Origin` (tests, CLI, Electron) and is browser-CSRF defence only — never restore state-on-connect behind it. Client side, `connected` flips on `auth_ack`, not socket open.
