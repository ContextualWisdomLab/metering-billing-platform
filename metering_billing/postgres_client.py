"""Commercially compatible PostgreSQL client for the durable ledger.

The adapter wraps BSD-licensed ``pg8000`` and exposes the small connection
surface used by ``PostgresUsageLedger`` and ``scripts/migrate_postgres.py``:
libpq-style DSN parsing, ``execute`` / ``cursor`` / ``transaction``, and a
stable foreign-key error type.  It does not import or recommend LGPL
``psycopg``.
"""

from __future__ import annotations

import getpass
import os
import re
import ssl
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.dbapi


COMMERCIAL_POSTGRES_CLIENT = "pg8000"
"""PyPI name of the commercially compatible PostgreSQL driver."""

DEFAULT_POSTGRES_PORT = 5432
UNIX_SOCKET_DIRECTORIES = ("/var/run/postgresql", "/tmp")
KEYWORD_DSN_PATTERN = re.compile(
    r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+))"
)
DOLLAR_QUOTE_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class ForeignKeyViolation(Exception):
    """Raised when PostgreSQL rejects a tenant-scoped foreign key."""


class PostgresConnection:
    """DB-API connection wrapper that preserves the durable-ledger session surface."""

    def __init__(self, raw_connection: Any) -> None:
        self._raw_connection = raw_connection
        self._transaction_depth = 0

    def execute(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> "PostgresCursor":
        """Execute one statement, or every statement in an unparameterized script."""
        statements = (sql,) if parameters is not None else split_sql_statements(sql)
        cursor: PostgresCursor | None = None
        for index, statement in enumerate(statements):
            cursor = PostgresCursor(self._raw_connection.cursor())
            try:
                cursor.execute(statement, parameters)
            except Exception:
                cursor.close()
                raise
            if index < len(statements) - 1:
                cursor.close()
        if cursor is None:
            raise ValueError("SQL text contains no executable statements")
        return cursor

    @contextmanager
    def cursor(self) -> Iterator["PostgresCursor"]:
        """Yield one cursor and close it when the caller is done."""
        cursor = PostgresCursor(self._raw_connection.cursor())
        try:
            yield cursor
        finally:
            cursor.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit one unit of work, or roll it back when the block fails.

        Nested callers receive a savepoint so the outer commercial transaction
        stays open.  Depth increment and ``SAVEPOINT`` share the ``finally``
        that restores depth so a refused savepoint cannot leak nesting.  The
        durable ledger itself avoids nesting through ``_transaction_active``.
        """
        nested = False
        try:
            self._transaction_depth += 1
            if self._transaction_depth > 1:
                nested = True
                self.execute("SAVEPOINT metering_billing_nested_transaction")
            try:
                yield
            except Exception:
                if nested:
                    self.execute(
                        "ROLLBACK TO SAVEPOINT metering_billing_nested_transaction"
                    )
                else:
                    self._raw_connection.rollback()
                raise
            else:
                if nested:
                    self.execute(
                        "RELEASE SAVEPOINT metering_billing_nested_transaction"
                    )
                else:
                    self._raw_connection.commit()
        finally:
            self._transaction_depth -= 1

    def commit(self) -> None:
        """Commit the current implicit transaction."""
        self._raw_connection.commit()

    def rollback(self) -> None:
        """Roll back the current implicit transaction."""
        self._raw_connection.rollback()

    def close(self) -> None:
        """Close the underlying commercially compatible connection."""
        self._raw_connection.close()

    def __enter__(self) -> "PostgresConnection":
        """Return this connection for a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Commit on success, roll back on failure, then close the session."""
        try:
            if exc_type is None:
                self._raw_connection.commit()
            else:
                self._raw_connection.rollback()
        finally:
            self.close()


class PostgresCursor:
    """Cursor that maps PostgreSQL foreign-key failures onto a stable type."""

    def __init__(self, raw_cursor: Any) -> None:
        self._raw_cursor = raw_cursor

    def execute(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> "PostgresCursor":
        """Execute one statement and translate foreign-key failures."""
        try:
            if parameters is None:
                self._raw_cursor.execute(sql)
            else:
                self._raw_cursor.execute(sql, parameters)
        except Exception as error:
            raise_translated_database_error(error)
        return self

    def fetchone(self) -> Any:
        """Return the next row or ``None``."""
        return self._raw_cursor.fetchone()

    def fetchall(self) -> list[Any]:
        """Return every remaining row."""
        return list(self._raw_cursor.fetchall())

    def close(self) -> None:
        """Close the underlying cursor."""
        self._raw_cursor.close()

    def __enter__(self) -> "PostgresCursor":
        """Return this cursor for a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Close the cursor when the block ends."""
        self.close()


def connect(dsn: str) -> PostgresConnection:
    """Open one commercially compatible PostgreSQL session from a libpq DSN."""
    return PostgresConnection(pg8000.dbapi.connect(**parse_postgres_dsn(dsn)))


def parse_postgres_dsn(dsn: str) -> dict[str, Any]:
    """Parse a libpq URI or keyword DSN into ``pg8000.dbapi.connect`` keywords."""
    stripped = dsn.strip()
    if not stripped:
        raise ValueError("PostgreSQL DSN must not be empty")
    if stripped.startswith("postgres://") or stripped.startswith("postgresql://"):
        fields = _parse_uri_dsn(stripped)
    else:
        fields = _parse_keyword_dsn(stripped)
    return _connect_keywords(fields)


def split_sql_statements(sql_text: str) -> tuple[str, ...]:
    """Split a PostgreSQL script on top-level semicolons.

    Semicolons inside standard quotes, dollar quotes, line comments, and
    block comments stay inside the current statement so a migration body can
    be applied through one ``execute`` call.
    """
    statements: list[str] = []
    current: list[str] = []
    index = 0
    length = len(sql_text)
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False
    dollar_tag: str | None = None
    while index < length:
        character = sql_text[index]
        nxt = sql_text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            current.append(character)
            if character == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            current.append(character)
            if character == "*" and nxt == "/":
                current.append(nxt)
                index += 2
                in_block_comment = False
                continue
            index += 1
            continue
        if dollar_tag is not None:
            if sql_text.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(character)
            index += 1
            continue
        if in_single_quote:
            current.append(character)
            if character == "'" and nxt == "'":
                current.append(nxt)
                index += 2
                continue
            if character == "'":
                in_single_quote = False
            index += 1
            continue
        if in_double_quote:
            current.append(character)
            if character == '"' and nxt == '"':
                current.append(nxt)
                index += 2
                continue
            if character == '"':
                in_double_quote = False
            index += 1
            continue
        if character == "-" and nxt == "-":
            current.extend(("-", "-"))
            in_line_comment = True
            index += 2
            continue
        if character == "/" and nxt == "*":
            current.extend(("/", "*"))
            in_block_comment = True
            index += 2
            continue
        if character == "'":
            in_single_quote = True
            current.append(character)
            index += 1
            continue
        if character == '"':
            in_double_quote = True
            current.append(character)
            index += 1
            continue
        dollar_match = DOLLAR_QUOTE_PATTERN.match(sql_text, index)
        if dollar_match is not None:
            dollar_tag = dollar_match.group(0)
            current.append(dollar_tag)
            index += len(dollar_tag)
            continue
        if character == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return tuple(statements)


def raise_translated_database_error(error: BaseException) -> None:
    """Re-raise ``error``, mapping PostgreSQL 23503 onto ``ForeignKeyViolation``."""
    if is_foreign_key_violation(error):
        raise ForeignKeyViolation(*getattr(error, "args", ())) from error
    raise error


def is_foreign_key_violation(error: BaseException) -> bool:
    """Return whether *error* is a PostgreSQL foreign-key failure.

    When a SQLSTATE is present, only ``23503`` is treated as a foreign-key
    violation.  Message-text matching is a fallback for drivers that omit
    the code, not a way to override a different SQLSTATE.
    """
    sqlstate = _postgres_sqlstate(error)
    if sqlstate is not None:
        return sqlstate == "23503"
    text = str(error).lower()
    return "foreign key" in text or "23503" in text


def _postgres_sqlstate(error: BaseException) -> str | None:
    """Read SQLSTATE from ``error.sqlstate`` or a pg8000 error payload."""
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return sqlstate
    args = getattr(error, "args", ())
    if not args:
        return None
    payload = args[0]
    if isinstance(payload, Mapping):
        code = payload.get("C")
        if isinstance(code, str) and code:
            return code
    return None


def _parse_uri_dsn(dsn: str) -> dict[str, str]:
    """Parse a ``postgres://`` or ``postgresql://`` URI into libpq fields."""
    parsed = urlparse(dsn)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    fields: dict[str, str] = {}
    database = unquote(parsed.path.lstrip("/")) or query.get("dbname") or query.get(
        "database", ""
    )
    if database:
        fields["dbname"] = database
    user = unquote(parsed.username) if parsed.username else query.get("user")
    if user:
        fields["user"] = user
    if parsed.password is not None:
        fields["password"] = unquote(parsed.password)
    elif "password" in query:
        fields["password"] = query["password"]
    host = parsed.hostname or query.get("host")
    if host:
        fields["host"] = host
    if parsed.port is not None:
        fields["port"] = str(parsed.port)
    elif "port" in query:
        fields["port"] = query["port"]
    if "sslmode" in query:
        fields["sslmode"] = query["sslmode"]
    return fields


def _parse_keyword_dsn(dsn: str) -> dict[str, str]:
    """Parse a space-separated ``key=value`` libpq DSN."""
    fields = {
        match.group(1): match.group(2) or match.group(3) or match.group(4)
        for match in KEYWORD_DSN_PATTERN.finditer(dsn)
    }
    if not fields:
        raise ValueError(f"unrecognized PostgreSQL DSN: {dsn}")
    return fields


def _connect_keywords(fields: Mapping[str, str]) -> dict[str, Any]:
    """Translate parsed libpq fields into ``pg8000.dbapi.connect`` keywords."""
    port = int(fields.get("port") or DEFAULT_POSTGRES_PORT)
    keywords: dict[str, Any] = {
        "user": fields.get("user") or getpass.getuser(),
        "database": fields.get("dbname") or fields.get("database"),
    }
    if "password" in fields:
        keywords["password"] = fields["password"]
    host = fields.get("host")
    if host and host.startswith("/"):
        keywords["unix_sock"] = f"{host}/.s.PGSQL.{port}"
    elif host:
        keywords["host"] = host
        keywords["port"] = port
    else:
        unix_sock = _existing_unix_socket(port)
        if unix_sock is not None:
            keywords["unix_sock"] = unix_sock
        else:
            keywords["host"] = "localhost"
            keywords["port"] = port
    if "sslmode" in fields:
        ssl_context = _ssl_context_for_mode(fields["sslmode"])
        if ssl_context is not None:
            keywords["ssl_context"] = ssl_context
    return keywords


def _ssl_context_for_mode(sslmode: str) -> ssl.SSLContext | None:
    """Translate one libpq ``sslmode`` into a ``pg8000`` TLS context.

    ``allow`` and ``prefer`` do not implement libpq's fallback negotiation.
    ``allow`` stays clear-text; ``prefer`` opens TLS without verifying the
    peer certificate, matching ``require``.
    """
    mode = sslmode.lower()
    if mode in {"disable", "allow"}:
        return None
    if mode in {"prefer", "require"}:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if mode == "verify-ca":
        context = ssl.create_default_context()
        context.check_hostname = False
        return context
    if mode == "verify-full":
        return ssl.create_default_context()
    raise ValueError(f"unsupported PostgreSQL sslmode: {sslmode}")


def _existing_unix_socket(port: int) -> str | None:
    """Return a local libpq unix socket path when one is already listening."""
    for directory in UNIX_SOCKET_DIRECTORIES:
        socket_path = f"{directory}/.s.PGSQL.{port}"
        if os.path.exists(socket_path):
            return socket_path
    return None
