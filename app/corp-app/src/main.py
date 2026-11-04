#!/usr/bin/env python3
"""
corp-app: Corporate application HTTP service.

Configuration is read exclusively from /etc/corp-app/config.ini.
This path is hardcoded and not overridable.

Imports SQLAlchemy, Pydantic, and PyMySQL.

Schema is owned by Alembic migrations in migrations/; this running service
issues no DDL at request time. Tables and seed data are created by
`alembic upgrade head` before this service is started.

Binds on 0.0.0.0 at [server] port (default 8080). The bind address is a
fixed property of the executable — not config-driven.
"""

from __future__ import annotations

import configparser
import json
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote_plus, urlsplit

# ---------------------------------------------------------------------------
# Hardcoded config path
# Not overridable: any mechanism that allows a different path would break
# the application's configuration contract.
# ---------------------------------------------------------------------------
CONFIG_PATH = "/etc/corp-app/config.ini"

# ---------------------------------------------------------------------------
# Corporate stack imports.
# If these fail, exit with a clear diagnostic rather than letting the
# service start in a broken state.
# ---------------------------------------------------------------------------
try:
    import sqlalchemy as sa
    from sqlalchemy import create_engine, text
except ImportError as _exc:
    print(
        f"FATAL: failed to import SQLAlchemy: {_exc}",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import pydantic
    from pydantic import BaseModel, ValidationError

    _PYDANTIC_MAJOR = int(pydantic.VERSION.split(".")[0])

    if _PYDANTIC_MAJOR >= 2:
        # Pydantic v2 API
        from pydantic import field_validator as _fv

        class RecordRequest(BaseModel):
            """Validate POST /record request body. Requires a non-empty string message."""

            message: str

            @_fv("message", mode="before")
            @classmethod
            def validate_message(cls, v: object) -> str:  # noqa: N805
                if not isinstance(v, str):
                    raise ValueError("message must be a string")
                if not v.strip():
                    raise ValueError("message must not be empty")
                return v

    else:
        # Pydantic v1 API (pre=True runs before type coercion)
        from pydantic import validator as _v

        class RecordRequest(BaseModel):  # type: ignore[no-redef]
            """Validate POST /record request body. Requires a non-empty string message."""

            message: str

            @_v("message", pre=True, always=True)
            def validate_message(cls, v: object) -> str:  # noqa: N805
                if not isinstance(v, str):
                    raise ValueError("message must be a string")
                if not v.strip():
                    raise ValueError("message must not be empty")
                return v

except ImportError as _exc:
    print(
        f"FATAL: failed to import Pydantic: {_exc}",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def load_config() -> configparser.ConfigParser:
    """
    Load application configuration from /etc/corp-app/config.ini.

    Exits non-zero with a clear diagnostic if the file is absent or if a
    required section is missing. The config path is never overridable.
    """
    cfg = configparser.ConfigParser()
    files_read = cfg.read(CONFIG_PATH)

    if not files_read:
        print(
            f"FATAL: Configuration file not found: {CONFIG_PATH}\n"
            "  The service will not start without a valid config file.",
            file=sys.stderr,
        )
        sys.exit(1)

    for section in ("database", "server"):
        if not cfg.has_section(section):
            print(
                f"FATAL: Required section [{section}] missing in {CONFIG_PATH}",
                file=sys.stderr,
            )
            sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# Database engine
# ---------------------------------------------------------------------------


def build_engine(cfg: configparser.ConfigParser) -> sa.Engine:
    """
    Build the SQLAlchemy connection pool from [database] config.

    Uses the PyMySQL dialect (mysql+pymysql) with charset=utf8mb4.

    user and password are percent-encoded with quote_plus so that special
    characters (e.g. @, /, #, +) in credentials do not corrupt the URL.

    pool_pre_ping ensures stale connections are detected before use.
    connect_timeout=5 causes fast failure on an unreachable database.
    """
    host = cfg.get("database", "host")
    port = cfg.getint("database", "port", fallback=3306)
    name = cfg.get("database", "name")
    user = cfg.get("database", "user")
    password = cfg.get("database", "password")

    # Percent-encode credentials: special characters in user/password break
    # URL parsing if embedded raw in the connection string.
    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_pre_ping=True,                   # validate connections before use
        connect_args={"connect_timeout": 5},  # fail fast on unreachable DB
    )


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------


def _json_default(obj: object) -> str:
    """Serialize types not handled by the standard json encoder."""
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class AppHandler(BaseHTTPRequestHandler):
    """
    ThreadingHTTPServer request handler for corp-app.

    Class-level attributes are injected by main() before the server starts;
    they are set once and only read by handler instances (thread-safe).

    engine:   SQLAlchemy Engine — shared connection pool across all threads.
    db_name:  database name reported in GET / response.
    db_host:  database host reported in GET / response.

    Route table:
      GET  /         → service info + 50 most-recent service_records rows
      POST /record   → Pydantic-validated insert, returns 201 with inserted row
      GET  /health   → DB connectivity check
      <known path, wrong method> → 405 JSON
      <any other path or method> → 404 JSON
    """

    # Class-level shared state — injected by main() at startup
    engine: sa.Engine
    db_name: str = ""
    db_host: str = ""

    # Route dispatch table: (method, path) → handler method name
    _ROUTES: dict[tuple[str, str], str] = {
        ("GET", "/"): "_handle_root",
        ("GET", "/health"): "_handle_health",
        ("POST", "/record"): "_handle_post_record",
    }
    # Paths the app recognises (used to distinguish 404 from 405)
    _KNOWN_PATHS: frozenset[str] = frozenset({"/", "/health", "/record"})

    # ------------------------------------------------------------------
    # Request dispatch
    #
    # Overrides BaseHTTPRequestHandler.handle_one_request so that any
    # HTTP method (PUT, DELETE, PATCH, …) produces a JSON 404 or 405
    # rather than the default text/html error page or 501 Not Implemented.
    # ------------------------------------------------------------------

    def handle_one_request(self) -> None:  # noqa: C901
        """Parse the request line and dispatch to the appropriate handler."""
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestversion = "HTTP/1.0"
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                # parse_request() sends its own error response on failure
                return
            self._dispatch()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

    def _dispatch(self) -> None:
        """Route the current request to its handler or emit 404/405.

        self.path may include a query string (e.g. '/health?verbose=1').
        urlsplit strips the query component so '/health?verbose=1' routes
        correctly to GET /health rather than falling through to a 404.
        """
        # Strip query string and fragment before matching; only the path matters.
        path = urlsplit(self.path).path
        handler_name = self._ROUTES.get((self.command, path))
        if handler_name:
            getattr(self, handler_name)()
        elif path in self._KNOWN_PATHS:
            self._send_json(
                405,
                {"detail": f"Method {self.command!r} not allowed on {path}"},
            )
        else:
            self._send_json(404, {"detail": f"Not found: {path}"})

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def send_error(  # type: ignore[override]
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """
        Override BaseHTTPRequestHandler.send_error to return JSON.

        The base implementation returns an HTML error page, which violates the
        contract that all corp-app responses carry Content-Type: application/json.
        This override is called by parse_request() for malformed request lines,
        oversized URIs, and other HTTP-protocol-level errors.
        """
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Unknown Error"

        error_message = message or phrase
        self.log_error("code %d, message %s", code, error_message)

        body_data: dict = {"detail": error_message}
        if explain:
            body_data["explain"] = explain
        body = json.dumps(body_data).encode("utf-8")

        self.send_response(code, error_message)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        # Suppress body for HEAD requests and responses that must not have a body
        if self.command != "HEAD" and code >= 200 and code not in (204, 304):
            self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        """Encode *data* as JSON and write an HTTP response with Content-Type: application/json."""
        body = json.dumps(data, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        """
        Read the request body using the Content-Length header.

        Returns empty bytes when Content-Length is absent, zero, or unparseable.
        The server's socket timeout prevents indefinite blocking.
        """
        raw = self.headers.get("Content-Length", "0")
        try:
            length = int(raw)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    def _handle_root(self) -> None:
        """
        GET / — service info and the 50 most-recent service_records rows.

        Response shape:
          {
            "service": "corp-app",
            "status": "operational",
            "database": {"status": "connected", "name": <name>, "host": <host>},
            "count": <total rows>,
            "records": [{"id": …, "message": …, "created_at": "YYYY-MM-DD HH:MM:SS"}, …]
          }

        On DB error: HTTP 503 with a JSON error body. The connection is
        always released via the 'with engine.connect()' context manager,
        including on error paths.
        """
        try:
            with self.engine.connect() as conn:
                total = conn.execute(
                    text("SELECT COUNT(*) FROM service_records")
                ).scalar()

                rows = conn.execute(
                    text(
                        "SELECT id, message, created_at "
                        "FROM service_records "
                        "ORDER BY id DESC "
                        "LIMIT 50"
                    )
                ).fetchall()

            records = [
                {
                    "id": row.id,
                    "message": row.message,
                    "created_at": row.created_at,  # serialized by _json_default
                }
                for row in rows
            ]

            self._send_json(
                200,
                {
                    "service": "corp-app",
                    "status": "operational",
                    "database": {
                        "status": "connected",
                        "name": self.db_name,
                        "host": self.db_host,
                    },
                    "count": total,
                    "records": records,
                },
            )

        except Exception as exc:
            self.log_error("DB error on GET /: %s", exc)
            self._send_json(
                503,
                {
                    "service": "corp-app",
                    "status": "degraded",
                    "database": {"status": "error"},
                    "detail": str(exc),
                },
            )

    def _handle_health(self) -> None:
        """
        GET /health — DB connectivity check and row count.

        On success: HTTP 200
          {"database": "connected", "count": <n>, "detail": "ok"}

        On failure: HTTP 503
          {"database": "error", "count": 0, "detail": "<exception message>"}
        """
        try:
            with self.engine.connect() as conn:
                count = conn.execute(
                    text("SELECT COUNT(*) FROM service_records")
                ).scalar()
            self._send_json(
                200,
                {"database": "connected", "count": count, "detail": "ok"},
            )
        except Exception as exc:
            self.log_error("Health check failed: %s", exc)
            self._send_json(
                503,
                {"database": "error", "count": 0, "detail": str(exc)},
            )

    def _handle_post_record(self) -> None:
        """
        POST /record — validate body with Pydantic, insert a row, return HTTP 201.

        Validation (Pydantic RecordRequest):
          - Body must be valid JSON.
          - Body must be a JSON object.
          - 'message' field must be present, a string, and non-empty.
          → Validation failure: HTTP 400 with JSON error body; no DB write.

        On success: HTTP 201
          {"id": <new id>, "message": <message>, "created_at": "YYYY-MM-DD HH:MM:SS"}

        On DB error: HTTP 503 with JSON error body.

        The running service issues no DDL — the service_records table must
        already exist (created by `alembic upgrade head`).
        """
        body_bytes = self._read_body()

        # Parse JSON — empty body is treated as an empty object and will
        # fail Pydantic validation (missing required 'message' field).
        try:
            body_json = json.loads(body_bytes) if body_bytes else {}
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"detail": f"Invalid JSON: {exc}"})
            return

        if not isinstance(body_json, dict):
            self._send_json(
                400, {"detail": "Request body must be a JSON object"}
            )
            return

        # Pydantic validation: non-empty string message
        try:
            record_req = RecordRequest(**body_json)
        except ValidationError as exc:
            try:
                errors = json.loads(exc.json())
            except Exception:
                errors = str(exc)
            self._send_json(400, {"detail": "Validation error", "errors": errors})
            return

        # Insert the validated record and read it back by its new PK.
        # engine.begin() auto-commits on clean exit, rolls back on exception.
        # LAST_INSERT_ID() is connection-scoped and returns the auto-increment
        # ID produced by the INSERT in the current connection.
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO service_records (message) VALUES (:message)"
                    ),
                    {"message": record_req.message},
                )
                last_id = conn.execute(
                    text("SELECT LAST_INSERT_ID()")
                ).scalar()
                row = conn.execute(
                    text(
                        "SELECT id, message, created_at "
                        "FROM service_records WHERE id = :id"
                    ),
                    {"id": last_id},
                ).fetchone()

            self._send_json(
                201,
                {
                    "id": row.id,
                    "message": row.message,
                    "created_at": row.created_at,  # serialized by _json_default
                },
            )

        except Exception as exc:
            self.log_error("DB error on POST /record: %s", exc)
            self._send_json(503, {"detail": f"Database error: {exc}"})

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        """Write access log entries to stdout."""
        print(
            f"{self.address_string()} [{self.log_date_time_string()}] "
            f"{fmt % args}"
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Load configuration, build the DB engine, and start the ThreadingHTTPServer."""
    cfg = load_config()

    db_host = cfg.get("database", "host")
    db_name = cfg.get("database", "name")
    server_port = cfg.getint("server", "port", fallback=8080)

    # Build the shared SQLAlchemy connection pool (corporate stack: PyMySQL + SQLAlchemy)
    engine = build_engine(cfg)

    # Inject shared state into the handler class before the server starts.
    # These are set once and only read by handler instances — safe under threading.
    AppHandler.engine = engine
    AppHandler.db_name = db_name
    AppHandler.db_host = db_host

    # Bind address is fixed at 0.0.0.0 — not config-driven.
    # [server] port comes from config; no bind_address key.
    bind_address = "0.0.0.0"

    server = ThreadingHTTPServer((bind_address, server_port), AppHandler)

    print(
        f"corp-app starting\n"
        f"  Listening : {bind_address}:{server_port}\n"
        f"  Database  : {db_name} @ {db_host}\n"
        f"  Config    : {CONFIG_PATH}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncorp-app shutting down cleanly.")
    finally:
        server.server_close()
        engine.dispose()  # return all pooled connections to the pool, then close them


if __name__ == "__main__":
    main()
