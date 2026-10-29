#!/usr/bin/env python3
"""corp-app.py — minimal corporate application service.

The value of this program is the image/Ansible wire-up pattern it
demonstrates, not its logic:

  * configuration comes from exactly one hardcoded path (G12);
  * the health endpoint binds loopback only, so the image opens no
    firewall port for the app;
  * the only external dependency (pymysql) is baked into the image
    (G9) — nothing is installed at runtime;
  * the service is enabled at first contact by Ansible, not in the
    image (G7), because the config file does not exist until then.

Run as root by systemd (see corp-app.service); no User=/Group= is set
so the mode-0640 root-owned config file is readable.
"""

import configparser
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# G12: the config path is hardcoded on purpose. It is the contract with
# the app_instance Ansible role, whose template task writes this exact
# path. No environment variable, no CLI argument — the hardcoding makes
# the interface explicit and a divergence impossible to hide.
CONFIG_PATH = "/etc/corp-app/config.ini"

# The health endpoint is loopback-only by design: it is polled on the
# host itself (by Ansible's uri check and by operators with curl) and
# nothing external may reach it. This is why the image opens no
# firewall port for the app. No config key configures this — it cannot
# be.
BIND_ADDRESS = "127.0.0.1"

# Short connect timeout so a failed database probe is reported in
# seconds instead of hanging the request.
CONNECT_TIMEOUT = 3


def fail(message):
    """Print a clear error to stderr (journal) and exit non-zero."""
    sys.exit(message)


# pymysql is a bake-time dependency (G9); a missing module means the
# image was built without it, so report it and exit instead of running
# degraded.
try:
    import pymysql
except ImportError as exc:
    fail(
        f"ERROR: cannot import pymysql ({exc}). python3-PyMySQL is a "
        "bake-time dependency (G9); rebuild the image rather than "
        "installing packages at runtime."
    )


def load_config():
    """Read CONFIG_PATH and return a parsed ConfigParser.

    This program never creates the config file or its directory — that
    is the app_instance role's job at first contact (G7).
    """
    try:
        parser = configparser.ConfigParser()
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError:
        fail(
            f"ERROR: {CONFIG_PATH} does not exist. The corp-app service "
            "cannot start until the app_instance Ansible role writes the "
            "runtime config (the image deliberately does not enable this "
            "service, G7)."
        )
    except PermissionError:
        fail(f"ERROR: {CONFIG_PATH} exists but is not readable by this process.")

    if not parser.has_section("database"):
        fail(f"ERROR: {CONFIG_PATH} is missing the [database] section.")
    return parser


def db_connect_kwargs(cfg):
    """Build pymysql.connect() kwargs from the [database] section.

    Keys read (the exact read-set of the G12 interface contract):
    host, port (default 3306), name, user, password.
    There is no bind_address key — loopback binding is hardcoded.
    """
    db = cfg["database"]
    missing = [key for key in ("host", "name", "user", "password") if not db.get(key)]
    if missing:
        fail(f"ERROR: {CONFIG_PATH} [database] is missing key(s): {', '.join(missing)}.")
    return {
        "host": db["host"],
        "port": int(db.get("port", "3306")),
        "database": db["name"],
        "user": db["user"],
        "password": db["password"],
        "connect_timeout": CONNECT_TIMEOUT,
    }


def probe_database(cfg):
    """Attempt one MariaDB connection; return (state, detail) for /health."""
    try:
        conn = pymysql.connect(**db_connect_kwargs(cfg))
    except Exception as exc:  # pymysql raises many distinct exception types
        return "error", str(exc)
    conn.close()
    return "connected", "ok"


def server_port(cfg):
    """Port from [server] (default 8080)."""
    if cfg.has_section("server"):
        return int(cfg["server"].get("port", "8080"))
    return 8080


class HealthHandler(BaseHTTPRequestHandler):
    """JSON-only request handler: /health or 404, nothing else."""

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            state, detail = probe_database(_CONFIG)
            if state == "connected":
                self._send_json(200, {"database": state, "detail": detail})
            else:
                self._send_json(503, {"database": state, "detail": detail})
        else:
            self._send_json(404, {"error": "not found"})


_CONFIG = None  # set by main(); read by the request handler


def main():
    global _CONFIG
    _CONFIG = load_config()
    port = server_port(_CONFIG)
    httpd = ThreadingHTTPServer((BIND_ADDRESS, port), HealthHandler)
    print(
        f"corp-app: health endpoint listening on http://{BIND_ADDRESS}:{port}/health",
        flush=True,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
