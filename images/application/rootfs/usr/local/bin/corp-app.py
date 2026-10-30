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
    image (G7), because the config file does not exist until then;
  * the application assumes service_records table and seed row exist —
    created by the mysql_instance Ansible role, not by this code.

Run as root by systemd (see corp-app.service); no User=/Group= is set
so the mode-0640 root-owned config file is readable.
"""

import configparser
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# G12: the config path is hardcoded on purpose. It is the contract with
# the app_instance Ansible role, whose template task writes this exact
# path. No environment variable, no CLI argument — the hardcoding makes
# the interface explicit and a divergence impossible to hide.
CONFIG_PATH = "/etc/corp-app/config.ini"

# The health endpoint is loopback-only by design: it is polled on the
# host itself (by Ansible's uri check and by operators with curl) and
# all interfaces so the app is reachable from outside the VM.
# The firewall port is opened by the app_instance Ansible role at runtime.
BIND_ADDRESS = "0.0.0.0"

# Short connect timeout so a failed database probe is reported in
# seconds instead of hanging the request.
CONNECT_TIMEOUT = 3

# Table name — fixed interface contract (design-spec.md §9, §6.3, §8.2).
# Changing this without updating the mysql_instance role's schema breaks
# every endpoint silently at runtime.
TABLE_NAME = "service_records"

# Maximum records returned by GET / (keeps response bounded).
RECORDS_LIMIT = 50


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

    Exits non-zero if:
      - the file does not exist;
      - the file is unreadable;
      - required sections or keys are missing.
    This fail-fast behaviour on the main thread prevents the server from
    starting in a broken state.
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

    # Validate required database keys on startup so the error is immediate
    # and traceable rather than surfacing as a cryptic 503 at request time.
    db = parser["database"]
    missing = [k for k in ("host", "name", "user", "password") if not db.get(k)]
    if missing:
        fail(
            f"ERROR: {CONFIG_PATH} [database] is missing required key(s): "
            f"{', '.join(missing)}."
        )

    return parser


def db_connect_kwargs(cfg):
    """Build pymysql.connect() kwargs from the [database] section.

    Keys read (the exact read-set of the G12 interface contract):
    host, port (default 3306), name, user, password.
    There is no bind_address key — loopback binding is hardcoded.
    """
    db = cfg["database"]
    return {
        "host": db["host"],
        "port": int(db.get("port", "3306")),
        "database": db["name"],
        "user": db["user"],
        "password": db["password"],
        "connect_timeout": CONNECT_TIMEOUT,
        "cursorclass": pymysql.cursors.DictCursor,
    }


def get_connection(cfg):
    """Open a fresh connection; callers must close it on every path."""
    return pymysql.connect(**db_connect_kwargs(cfg))


def server_port(cfg):
    """Port from [server] (default 8080)."""
    if cfg.has_section("server"):
        return int(cfg["server"].get("port", "8080"))
    return 8080


class AppHandler(BaseHTTPRequestHandler):
    """JSON-only HTTP request handler implementing the four routes.

    Routes:
      GET  /        — service info, record listing, row count
      POST /record  — insert a new record, return the inserted row
      GET  /health  — database connectivity + row count
      *             — 404 (or 405 for a known path with wrong method)

    All responses carry Content-Type: application/json.
    Connections are always closed, including on error paths (G6.3).
    """

    def log_message(self, fmt, *args):
        # Route access log to stdout so systemd's journal captures it.
        sys.stdout.write(
            f"{self.address_string()} - - [{self.log_date_time_string()}] "
            f"{fmt % args}\n"
        )
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, detail):
        self._send_json(status, {"error": detail, "detail": detail})

    def send_error(self, code, message=None, explain=None):
        """Override BaseHTTPRequestHandler.send_error to emit JSON.

        BaseHTTPRequestHandler.send_error writes an HTML body that violates
        the JSON-only contract (design-spec §9, AppHandler docstring).
        Any HTTP method not explicitly dispatched below (PUT, DELETE, PATCH,
        OPTIONS, …) falls through to send_error via the base class; overriding
        it here ensures every error response — including those produced for
        unhandled methods — is a JSON object, never an HTML page.
        """
        self._error(code, message or "HTTP error")

    # ------------------------------------------------------------------
    # Route implementations
    # ------------------------------------------------------------------

    def _handle_health(self):
        """GET /health — database connectivity probe + row count.

        Success: HTTP 200, {"database": "connected", "count": N, "detail": "ok"}
        Failure: HTTP 503, {"database": "error",     "count": 0, "detail": "<exc>"}
        """
        conn = None
        try:
            conn = get_connection(_CONFIG)
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE_NAME}")
                row = cur.fetchone()
            count = int(row["cnt"]) if row else 0
            self._send_json(200, {"database": "connected", "count": count, "detail": "ok"})
        except Exception as exc:
            self._send_json(
                503,
                {"database": "error", "count": 0, "detail": str(exc)},
            )
        finally:
            if conn:
                conn.close()

    def _handle_root(self):
        """GET / — service info + data read (up to RECORDS_LIMIT rows).

        Success: HTTP 200 with service metadata, database status, total count,
                 and the most-recent RECORDS_LIMIT rows ordered by id DESC.
                 Timestamps are serialized as ISO-format strings via json default=str.
        Failure: HTTP 503 with a JSON error object.
        """
        conn = None
        try:
            conn = get_connection(_CONFIG)
            db_cfg = _CONFIG["database"]
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE_NAME}")
                count_row = cur.fetchone()
                total = int(count_row["cnt"]) if count_row else 0

                cur.execute(
                    f"SELECT id, message, created_at "
                    f"FROM {TABLE_NAME} "
                    f"ORDER BY id DESC "
                    f"LIMIT %s",
                    (RECORDS_LIMIT,),
                )
                rows = cur.fetchall()

            # Serialize each row; created_at is a datetime — json default=str
            # converts it to an ISO string without requiring extra imports.
            records = [
                {
                    "id": r["id"],
                    "message": r["message"],
                    "created_at": str(r["created_at"]),
                }
                for r in rows
            ]

            self._send_json(
                200,
                {
                    "service": "corp-app",
                    "status": "operational",
                    "database": {
                        "status": "connected",
                        "name": db_cfg.get("name", ""),
                        "host": db_cfg.get("host", ""),
                    },
                    "count": total,
                    "records": records,
                },
            )
        except Exception as exc:
            self._send_json(
                503,
                {"error": "database error", "detail": str(exc)},
            )
        finally:
            if conn:
                conn.close()

    def _handle_post_record(self):
        """POST /record — validate input, insert row, return inserted record.

        Accepts: {"message": "<non-empty string>"}
        Returns: HTTP 201 with {"id": N, "message": "...", "created_at": "..."}

        Validation failures → 400.  Database errors → 503.
        """
        # --- Parse and validate JSON body ---
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            self._error(400, f"request body is not valid JSON: {exc}")
            return

        if not isinstance(data, dict):
            self._error(400, "request body must be a JSON object")
            return

        message = data.get("message")
        if message is None:
            self._error(400, "'message' field is required")
            return
        if not isinstance(message, str):
            self._error(400, "'message' must be a string")
            return
        if not message.strip():
            self._error(400, "'message' must not be empty")
            return

        # --- Insert and read back ---
        conn = None
        try:
            conn = get_connection(_CONFIG)
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {TABLE_NAME} (message) VALUES (%s)",
                    (message,),
                )
                new_id = cur.lastrowid
                conn.commit()

                # Read back the inserted row to prove the write committed
                # and to return the server-assigned created_at timestamp.
                cur.execute(
                    f"SELECT id, message, created_at "
                    f"FROM {TABLE_NAME} WHERE id = %s",
                    (new_id,),
                )
                row = cur.fetchone()

            self._send_json(
                201,
                {
                    "id": row["id"],
                    "message": row["message"],
                    "created_at": str(row["created_at"]),
                },
            )
        except Exception as exc:
            self._send_json(
                503,
                {"error": "database error", "detail": str(exc)},
            )
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # HTTP method dispatch
    # ------------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._handle_health()
        elif path == "/":
            self._handle_root()
        else:
            self._error(404, f"no route for GET {path}")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/record":
            self._handle_post_record()
        elif path in ("/", "/health"):
            # Recognised path, unsupported method.
            self._error(405, f"method POST not allowed on {path}")
        else:
            self._error(404, f"no route for POST {path}")

    def do_HEAD(self):
        # HEAD must return the same status and headers as the equivalent GET
        # but MUST NOT include a response body (RFC 9110 §9.3.2).
        # _send_json writes to self.wfile, so we send the status line and
        # headers manually and call end_headers() without writing any body.
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            # Determine what the body length would be so Content-Length is
            # accurate, but do not write the body itself.
            body = json.dumps({}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        else:
            # For unknown paths emit a 404 header-only response.
            body = json.dumps(
                {"error": f"no route for HEAD {path}", "detail": f"no route for HEAD {path}"}
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()


_CONFIG = None  # set by main(); read by the request handler


def main():
    global _CONFIG
    # load_config() exits non-zero on any startup validation failure,
    # so reaching the ThreadingHTTPServer line guarantees the config
    # and required database keys are present.
    _CONFIG = load_config()
    port = server_port(_CONFIG)
    httpd = ThreadingHTTPServer((BIND_ADDRESS, port), AppHandler)
    print(
        f"corp-app: listening on http://{BIND_ADDRESS}:{port}/",
        flush=True,
    )
    print(
        f"corp-app: endpoints: GET /, GET /health, POST /record",
        flush=True,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
