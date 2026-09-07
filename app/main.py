from __future__ import annotations

import os
import socket
from http.server import ThreadingHTTPServer

from .auth import ensure_password_configured
from .config import AGENT_BIN, HOST, PORT, agent_install_hint
from .http_handler import Handler
from .store import load_state


def _make_server(host: str, port: int) -> ThreadingHTTPServer:
    want_dual = host in ("0.0.0.0", "::", "")

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        # Windows SO_REUSEADDR lets two processes bind the same port; the
        # stale one accepts then closes, which Chrome shows as ERR_EMPTY_RESPONSE.
        allow_reuse_address = os.name != "nt"
        address_family = socket.AF_INET6 if want_dual else socket.AF_INET

        def server_bind(self) -> None:
            if self.address_family == socket.AF_INET6:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    bind_host = "::" if want_dual else host
    try:
        return Server((bind_host, port), Handler)
    except OSError:
        if not want_dual:
            raise

        class V4Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = os.name != "nt"
            address_family = socket.AF_INET

        return V4Server(("0.0.0.0", port), Handler)


def main() -> None:
    if not AGENT_BIN.exists():
        print(agent_install_hint(), flush=True)
        print(f"Looked for: {AGENT_BIN}", flush=True)
        print("Web UI will still start; sending a prompt will fail until agent is installed.\n", flush=True)
    ensure_password_configured()
    httpd = _make_server(HOST, PORT)
    print(f"listening on http://{HOST}:{PORT}/login", flush=True)
    print(f"open:      http://127.0.0.1:{PORT}/login", flush=True)
    print(f"agent:     {AGENT_BIN}", flush=True)
    print(f"workspace: {load_state()['workspace']}", flush=True)
    print("Set AGENT_WEB_PASSWORD to change the web login password.", flush=True)
    print("Paste CURSOR_API_KEY in the page settings. Do not put the key in chat.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
