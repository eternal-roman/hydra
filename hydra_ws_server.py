"""Hydra WebSocket Server."""
import subprocess
import json
import time
import os
import asyncio
import threading
import secrets
import sqlite3
from pathlib import Path
from typing import Dict, Any
import hydra_auth

# ═══════════════════════════════════════════════════════════════
# WEBSOCKET BROADCAST SERVER (for React Dashboard)
# ═══════════════════════════════════════════════════════════════

class DashboardBroadcaster:
    """Async WebSocket server that broadcasts agent state to dashboard clients.

    Phase 6 refactor (v2.10.0): adds message-type discrimination for the
    backtest observer, experiment library, and review stream.

    Outbound:
      - `broadcast(state)` — live per-tick state. Sends BOTH the legacy
        raw state dict and the wrapped `{"type": "state", "data": state}`
        form. Existing dashboards keep reading raw; the current dashboard
        reads wrapped.
      - `broadcast_message(type, payload)` — new type-discriminated
        message (e.g., backtest_progress). Always wrapped; legacy
        dashboards ignore unknown shapes.

    Inbound (Phase 6 additive):
      - `register_handler(type, fn)` — route JSON messages matching
        `{"type": type, ...}` to `fn(payload) -> Optional[dict]`. The
        return dict is sent back as `{"type": f"{type}_ack", ...reply}`.
      - Unknown message types are silently ignored (we don't want the
        dashboard DoS'ing the agent via malformed messages).

    Threading: the asyncio loop runs in a daemon thread. `broadcast_*`
    is thread-safe (uses run_coroutine_threadsafe). Handlers execute
    on the asyncio loop thread, so long work should be handed off
    (handlers in this codebase return quickly — they queue into
    BacktestWorkerPool).
    """

    # Origins allowed to initiate browser-side WS connections. Non-browser
    # clients (tests, CLI tools) send no Origin header and are permitted.
    ALLOWED_ORIGIN_PREFIXES = (
        "http://localhost:", "http://127.0.0.1:",
        "https://localhost:", "https://127.0.0.1:",
    )

    # Token file paths — written at startup so the dashboard (served by
    # Vite at dashboard/public/*) can fetch it via HTTP, and tests/CLI
    # tools can read it directly from the Hydra root.
    #
    # The dist/ entry covers the production build path (Electron-wrapped
    # desktop dashboard served from dashboard/dist/). Without it, dist/
    # carries whatever token was bundled at `npm run build` time, which
    # never matches the live agent's per-process token — every dispatch
    # request (Research panes, etc.) replies auth_required while
    # broadcasts (LIVE tab) keep working because they bypass auth.
    # _write_token_files() is gated on a sentinel-file check (e.g.
    # dist/index.html) so dev-only checkouts that never built dist/
    # don't get a stray dashboard/dist/ directory.
    TOKEN_FILES = (
        Path("hydra_ws_token.json"),
        Path("dashboard/public/hydra_ws_token.json"),
        Path("dashboard/dist/hydra_ws_token.json"),
    )

    # Token file paths that should only be written if a sentinel exists.
    # Maps the token path to the sentinel that proves the surrounding
    # build artifact is present. Skipped silently when the sentinel is
    # absent — keeps dev-only checkouts clean.
    TOKEN_FILE_SENTINELS = {
        Path("dashboard/dist/hydra_ws_token.json"):
            Path("dashboard/dist/index.html"),
    }

    # Loopback by default, and both directions now require the per-process
    # token — see the `pending`/`clients` split in __init__ for the outbound
    # half. The dashboard and Electron wrapper are local, so loopback costs
    # nothing. Set HYDRA_WS_HOST=0.0.0.0 to deliberately expose it (e.g.
    # behind a reverse proxy); the auth handshake, not the bind address, is
    # what keeps account state private.
    DEFAULT_HOST = os.environ.get("HYDRA_WS_HOST", "127.0.0.1")

    def __init__(self, host: str = None, port: int = 8765):
        if host is None:
            host = self.DEFAULT_HOST
        self.host = host
        self.port = port
        # `clients` receives broadcasts and the state snapshot; a socket only
        # graduates into it after a successful auth handshake. `pending` holds
        # freshly accepted sockets that have not proved anything yet.
        #
        # Binding to loopback (v2.32.1) narrowed WHO could reach the port, but
        # the outbound path still had no credential at all: _handler used to
        # add every socket to `clients` and immediately push `latest_state`,
        # which carries balance, per-asset holdings, portfolio drawdown, open
        # positions and the last 20 order-journal entries. The token gated
        # inbound commands only. Any local process — and under
        # HYDRA_WS_HOST=0.0.0.0 any host — could read the whole account by
        # opening a socket and never sending a byte.
        self.clients = set()
        self.pending = set()
        self.latest_state = {}
        self._loop = None
        self._thread = None
        self._handlers: Dict[str, Any] = {}
        self.active_agents: Dict[str, Dict[str, Any]] = {}
        self.next_agent_port = 8766
        self.production_mode = os.environ.get("HYDRA_PRODUCTION", "0") == "1"
        # Freshly generated per-process token. Inbound command messages
        # must include this exact value in their `auth` field — defends
        # the dispatch channel against dashboard-XSS-chain attacks even
        # though the socket is bound to 127.0.0.1.
        self.auth_token = secrets.token_hex(32)
        # Seconds a socket may sit unauthenticated before it is closed.
        self.auth_grace_s = float(os.environ.get("HYDRA_WS_AUTH_GRACE_S", "10"))
        self._write_token_files()
        
        # Register core auth handlers
        self.register_handler("login", self._handle_login)
        self.register_handler("save_keys", self._handle_save_keys)
        self.register_handler("start_agent", self._handle_start_agent)
        self.register_handler("stop_agent", self._handle_stop_agent)

    def _handle_login(self, payload: dict) -> dict:
        username = payload.get("username")
        password = payload.get("password")
        if not username or not password:
            return {"success": False, "error": "Missing credentials"}
            
        user = hydra_auth.authenticate_user(username, password)
        if user:
            token = hydra_auth.create_access_token(
                {"sub": user["username"], "role": user["role"]}
            )
            return {"success": True, "token": token, "user": user}
        return {"success": False, "error": "Invalid credentials"}
        
    def _handle_save_keys(self, payload: dict) -> dict:
        jwt_token = payload.get("jwt")
        if not jwt_token:
            return {"success": False, "error": "Unauthorized"}
            
        user_info = hydra_auth.verify_token(jwt_token)
        if not user_info:
            return {"success": False, "error": "Invalid token"}
            
        username = user_info.get("sub")
        conn = sqlite3.connect("hydra_users.db")
        c = conn.cursor()
        c.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        )
        row = c.fetchone()
        conn.close()
        
        if not row:
            return {"success": False, "error": "User not found"}
            
        user_id = row[0]
        api_key = payload.get("api_key")
        api_secret = payload.get("api_secret")
        exchange = payload.get("exchange", "kraken")
        
        if not api_key or not api_secret:
            return {"success": False, "error": "Missing API key or secret"}
            
        success = hydra_auth.save_api_keys(user_id, exchange, api_key, api_secret)
        return {"success": success}

    def _handle_start_agent(self, payload: dict) -> dict:
        jwt_token = payload.get("jwt")
        if not jwt_token: return {"success": False, "error": "Unauthorized"}
        user_info = hydra_auth.verify_token(jwt_token)
        if not user_info: return {"success": False, "error": "Invalid token"}
        
        username = user_info.get("sub")
        if username in self.active_agents:
            # Agent already running for this user
            return {"success": True, "port": self.active_agents[username]["port"]}
            
        port = self.next_agent_port
        self.next_agent_port += 1
        
        # Spawn the agent process
        try:
            # Pass --user to trigger API key injection, and --ws-port for its isolated UI feed
            cmd = ["python", "hydra_agent.py", "--user", username, "--ws-port", str(port)]
            if payload.get("paper"):
                cmd.append("--paper")
            if payload.get("resume"):
                cmd.append("--resume")
                
            proc = subprocess.Popen(cmd)
            self.active_agents[username] = {"process": proc, "port": port}
            return {"success": True, "port": port}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _handle_stop_agent(self, payload: dict) -> dict:
        jwt_token = payload.get("jwt")
        if not jwt_token: return {"success": False, "error": "Unauthorized"}
        user_info = hydra_auth.verify_token(jwt_token)
        if not user_info: return {"success": False, "error": "Invalid token"}
        
        username = user_info.get("sub")
        if username not in self.active_agents:
            return {"success": False, "error": "Agent not running"}
            
        proc_info = self.active_agents.pop(username)
        try:
            proc_info["process"].terminate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _write_token_files(self) -> None:
        payload = json.dumps({"token": self.auth_token})
        for path in self.TOKEN_FILES:
            sentinel = self.TOKEN_FILE_SENTINELS.get(path)
            if sentinel is not None and not sentinel.exists():
                # Dev-only checkout (no production build); skip silently
                # so we don't materialize an otherwise-empty dist/ dir.
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    # Windows semantics differ; best-effort only.
                    pass
            except OSError as e:
                print(f"  [WS] Failed to write token file {path}: {e}")

    def start(self):
        """Start WebSocket server in a background thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self):
        try:
            import websockets
            async with websockets.serve(self._handler, self.host, self.port):
                print(f"  [WS] Dashboard server running on ws://{self.host}:{self.port}")
                self._serve_future = asyncio.Future()
                await self._serve_future  # run until stop() is called
        except ImportError:
            print("  [WS] websockets package not installed — dashboard feed disabled")
            print("  [WS] Install with: pip install websockets")

    def stop(self):
        """Stop the server and gracefully terminate all managed agent processes."""
        print("\n[WS] Terminating all active agent sub-processes...")
        for username, info in list(self.active_agents.items()):
            try:
                proc = info["process"]
                print(f"  [WS] Terminating agent for user: {username} (PID: {proc.pid})")
                proc.terminate()
            except Exception as e:
                print(f"  [WS] Error terminating agent for {username}: {e}")
        
        self.active_agents.clear()
        
        if hasattr(self, '_loop') and self._loop and hasattr(self, '_serve_future'):
            try:
                self._loop.call_soon_threadsafe(self._serve_future.set_result, None)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _origin_allowed(self, origin: str) -> bool:
        """Browser-CSRF defence only — NOT the authentication boundary.

        Deliberately fails open for a missing Origin so non-browser clients
        (tests, CLI, the Electron wrapper) still connect. That is safe only
        because the auth handshake in `_handler` now gates every byte of
        account state; before it did not, and this fail-open was the whole
        front door. Do not restore state-on-connect behind this check.
        """
        if not origin:
            return True  # non-browser client (tests, CLI)
        return any(origin.startswith(p) for p in self.ALLOWED_ORIGIN_PREFIXES)

    def _client_authenticated(self, msg: dict) -> bool:
        """True when `msg` carries a valid credential for this process."""
        if self.production_mode:
            jwt_token = msg.get("jwt")
            return bool(jwt_token and hydra_auth.verify_token(jwt_token))
        provided = msg.get("auth", "")
        return (isinstance(provided, str)
                and secrets.compare_digest(provided, self.auth_token))

    def _request_origin(self, websocket) -> str:
        # Version-compat shim: `websockets` 10.x exposes `request_headers`,
        # 11.x+ exposes `request.headers`. Non-Mapping types also exist.
        headers = getattr(websocket, "request_headers", None)
        if headers is None:
            req = getattr(websocket, "request", None)
            headers = getattr(req, "headers", None) if req else None
        if headers is None:
            return ""
        try:
            return headers.get("Origin", "") or headers.get("origin", "")
        except Exception:
            return ""

    async def _handler(self, websocket):
        origin = self._request_origin(websocket)
        if not self._origin_allowed(origin):
            print(f"  [WS] Rejected connection from origin: {origin!r}")
            try:
                await websocket.close(code=1008, reason="origin not allowed")
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            return
        # Accepted but unproven. No state is sent and no broadcast reaches it
        # until it authenticates.
        self.pending.add(websocket)
        reaper = asyncio.create_task(self._reap_unauthenticated(websocket))
        try:
            async for raw in websocket:
                try:
                    await self._dispatch_inbound(raw, websocket)
                except Exception as e:
                    # Never let a malformed message break the connection.
                    print(f"  [WS] inbound dispatch error: {type(e).__name__}: {e}")
        except Exception as e:
            if not isinstance(e, (ConnectionError, OSError)):
                print(f"  [WS] Client handler error: {type(e).__name__}: {e}")
        finally:
            reaper.cancel()
            self.pending.discard(websocket)
            was_client = websocket in self.clients
            self.clients.discard(websocket)
            if was_client:
                print(f"  [WS] Dashboard client disconnected "
                      f"({len(self.clients)} total)")

    async def _reap_unauthenticated(self, websocket):
        """Close a socket that never authenticates within the grace window."""
        try:
            await asyncio.sleep(self.auth_grace_s)
        except asyncio.CancelledError:
            return
        if websocket in self.pending:
            self.pending.discard(websocket)
            print(f"  [WS] Closing unauthenticated client after "
                  f"{self.auth_grace_s:.0f}s")
            try:
                await websocket.close(code=1008, reason="auth required")
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

    async def _promote_client(self, websocket):
        """Move an authenticated socket into `clients` and send it state."""
        if websocket in self.clients:
            return
        self.pending.discard(websocket)
        self.clients.add(websocket)
        print(f"  [WS] Dashboard client authenticated "
              f"({len(self.clients)} total)")
        if self.latest_state:
            # Legacy raw + wrapped, matching the pre-auth-gate contract.
            await websocket.send(json.dumps(self.latest_state))
            await websocket.send(json.dumps({
                "type": "state", "data": self.latest_state,
            }))

    async def _dispatch_inbound(self, raw, websocket):
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return  # silently ignore non-JSON
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")

        # Explicit handshake: promotes the socket into `clients` so it starts
        # receiving state and broadcasts. Handled before the handler lookup
        # because it needs the websocket itself, which handlers never see.
        if msg_type == "auth":
            ok = self._client_authenticated(msg)
            if ok:
                await self._promote_client(websocket)
            try:
                await websocket.send(json.dumps({
                    "type": "auth_ack",
                    "success": ok,
                    **({} if ok else {"error": "auth_required"}),
                }))
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            return

        handler = self._handlers.get(msg_type) if msg_type else None
        if handler is None:
            return
        # Authentication logic
        if msg_type == "login":
            is_authenticated = True  # Login endpoint is public
        else:
            is_authenticated = self._client_authenticated(msg)
            # A command carrying a valid credential also promotes the socket,
            # so a client that goes straight to commands still gets state.
            if is_authenticated:
                await self._promote_client(websocket)

        if not is_authenticated:
            try:
                await websocket.send(json.dumps({
                    "type": f"{msg_type}_ack",
                    "success": False,
                    "error": "auth_required",
                }))
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            return
        payload = {k: v for k, v in msg.items() if k not in ("type", "auth")}
        try:
            reply = handler(payload)
        except Exception as e:
            reply = {"success": False, "error": f"{type(e).__name__}: {e}"}
        if reply is None:
            return
        try:
            ack_type = f"{msg_type}_ack"
            await websocket.send(json.dumps({"type": ack_type, **reply}))
        except Exception:
            # Client likely dropped mid-send — next broadcast will reap it
            pass

    def register_handler(self, msg_type: str, fn) -> None:
        """Route inbound messages with matching `type` to `fn(payload)`."""
        self._handlers[msg_type] = fn

    def broadcast(self, state: dict):
        """Broadcast live tick state to all connected dashboard clients.

        Emits BOTH the legacy raw state and the wrapped `{type: "state",
        data}` form so older and current dashboard readers both work.
        """
        self.latest_state = state
        if not (self._loop and self.clients):
            return
        wrapped = json.dumps({"type": "state", "data": state})
        raw = json.dumps(state)
        for client in list(self.clients):
            asyncio.run_coroutine_threadsafe(
                self._safe_send(client, raw), self._loop
            )
            asyncio.run_coroutine_threadsafe(
                self._safe_send(client, wrapped), self._loop
            )

    def broadcast_message(self, msg_type: str, payload: dict):
        """Emit a typed message (never wrapped as `state`). Always uses
        the `{type, ...payload}` format.  Safe to call from any thread.
        """
        if not (self._loop and self.clients):
            return
        msg = json.dumps({"type": msg_type, **payload})
        for client in list(self.clients):
            asyncio.run_coroutine_threadsafe(
                self._safe_send(client, msg), self._loop
            )

    async def _safe_send(self, client, msg):
        try:
            await client.send(msg)
        except Exception:
            self.clients.discard(client)

if __name__ == "__main__":
    print("========================================")
    print(" HYDRA WebSocket Manager (Multi-Tenant)")
    print("========================================")
    print()
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # Initialize the database to ensure tables exist
    hydra_auth.init_db()
    
    port = int(os.environ.get("HYDRA_WS_PORT", 8765))
    # Honour DEFAULT_HOST (loopback unless HYDRA_WS_HOST says otherwise)
    # rather than hardcoding a public bind here.
    server = DashboardBroadcaster(port=port)
    server.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WS] Shutting down manager...")
        server.stop()
