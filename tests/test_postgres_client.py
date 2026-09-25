"""Tests for the commercially compatible PostgreSQL client adapter."""

from __future__ import annotations

import unittest
from unittest import mock

from metering_billing.postgres_client import (
    ForeignKeyViolation,
    PostgresConnection,
    PostgresCursor,
    connect,
    is_foreign_key_violation,
    parse_postgres_dsn,
    raise_translated_database_error,
    split_sql_statements,
)


class FakeCursor:
    """Minimal DB-API cursor used to exercise the adapter without a server."""

    def __init__(self, owner: "FakeConnection") -> None:
        self.owner = owner
        self.closed = False
        self.queries: list[tuple[str, object]] = []
        self._rows: list[tuple[object, ...]] = [(1,)]

    def execute(self, sql: str, parameters: object = None) -> None:
        """Record one statement or raise the owner-configured error."""
        if self.owner.execute_error is not None:
            raise self.owner.execute_error
        self.queries.append((sql, parameters))
        self.owner.queries.append((sql, parameters))

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first configured row."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every configured row."""
        return list(self._rows)

    def close(self) -> None:
        """Mark the cursor closed."""
        self.closed = True

    def __enter__(self) -> "FakeCursor":
        """Return this cursor."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close on block exit."""
        self.close()


class FakeConnection:
    """Minimal DB-API connection that records commit, rollback, and close."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.queries: list[tuple[str, object]] = []
        self.execute_error: BaseException | None = None
        self._cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        """Open one recorded cursor."""
        cursor = FakeCursor(self)
        self._cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    def rollback(self) -> None:
        """Record a rollback."""
        self.rollbacks += 1

    def close(self) -> None:
        """Record a close."""
        self.closed = True


class ForeignKeyError(Exception):
    """Stand-in driver error with an optional SQLSTATE."""

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class PostgresClientUnitTests(unittest.TestCase):
    """Cover DSN parsing, script splitting, and session adapter branches."""

    def test_keyword_dsn_parses_quoted_and_unquoted_fields(self) -> None:
        """A libpq keyword DSN keeps user, password, host, port, and database."""
        keywords = parse_postgres_dsn(
            "dbname=metering_billing_test user=postgres "
            "password='s3 cret' host=127.0.0.1 port=5432"
        )
        self.assertEqual(
            keywords,
            {
                "user": "postgres",
                "password": "s3 cret",
                "host": "127.0.0.1",
                "port": 5432,
                "database": "metering_billing_test",
            },
        )

    def test_uri_dsn_parses_credentials_and_query_overrides(self) -> None:
        """URI userinfo and query parameters both populate connect keywords."""
        keywords = parse_postgres_dsn(
            "postgresql://billing%2Fuser:p%40ss@postgres_database:6543/"
            "metering_billing?sslmode=disable"
        )
        self.assertEqual(keywords["user"], "billing/user")
        self.assertEqual(keywords["password"], "p@ss")
        self.assertEqual(keywords["host"], "postgres_database")
        self.assertEqual(keywords["port"], 6543)
        self.assertEqual(keywords["database"], "metering_billing")

    def test_uri_dsn_reads_query_credentials_and_legacy_scheme(self) -> None:
        """Query-only userinfo and postgres:// still produce connect keywords."""
        keywords = parse_postgres_dsn(
            "postgres://127.0.0.1/fallback?user=query_user"
            "&password=query_secret&port=5544&dbname=from_query"
        )
        self.assertEqual(keywords["user"], "query_user")
        self.assertEqual(keywords["password"], "query_secret")
        self.assertEqual(keywords["port"], 5544)
        self.assertEqual(keywords["database"], "fallback")

        query_database = parse_postgres_dsn(
            "postgresql://127.0.0.1/?database=query_database&user=only_user"
        )
        self.assertEqual(query_database["database"], "query_database")
        self.assertEqual(query_database["user"], "only_user")

    def test_keyword_dsn_uses_unix_socket_for_directory_host(self) -> None:
        """A keyword host that is a directory becomes the libpq socket path."""
        keywords = parse_postgres_dsn(
            "dbname=metering_billing_test user=postgres host=/var/run/postgresql port=5432"
        )
        self.assertEqual(keywords["unix_sock"], "/var/run/postgresql/.s.PGSQL.5432")
        self.assertNotIn("host", keywords)

    def test_uri_dsn_uses_unix_socket_when_host_is_a_directory(self) -> None:
        """A directory host becomes the libpq unix-socket path."""
        keywords = parse_postgres_dsn(
            "postgresql:///metering_billing_test?host=/tmp&port=5433"
        )
        self.assertEqual(keywords["database"], "metering_billing_test")
        self.assertEqual(keywords["unix_sock"], "/tmp/.s.PGSQL.5433")
        self.assertNotIn("host", keywords)

    def test_keyword_dsn_uses_local_socket_when_one_exists(self) -> None:
        """Omitting host prefers an existing libpq unix socket."""
        with mock.patch(
            "metering_billing.postgres_client.os.path.exists",
            side_effect=lambda path: path == "/var/run/postgresql/.s.PGSQL.5432",
        ):
            keywords = parse_postgres_dsn("dbname=metering_billing_usage_repo_test")
        self.assertEqual(keywords["database"], "metering_billing_usage_repo_test")
        self.assertEqual(keywords["unix_sock"], "/var/run/postgresql/.s.PGSQL.5432")
        self.assertNotIn("host", keywords)

    def test_keyword_dsn_falls_back_to_localhost_without_a_socket(self) -> None:
        """A dbname-only DSN uses localhost TCP when no unix socket is present."""
        with mock.patch(
            "metering_billing.postgres_client.os.path.exists", return_value=False
        ), mock.patch(
            "metering_billing.postgres_client.getpass.getuser",
            return_value="ledger_user",
        ):
            keywords = parse_postgres_dsn('dbname="quoted_db"')
        self.assertEqual(
            keywords,
            {
                "user": "ledger_user",
                "database": "quoted_db",
                "host": "localhost",
                "port": 5432,
            },
        )

    def test_empty_or_unknown_dsn_fails_closed(self) -> None:
        """Blank and unparseable DSNs never open a silent default session."""
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            parse_postgres_dsn("   ")
        with self.assertRaisesRegex(ValueError, "unrecognized PostgreSQL DSN"):
            parse_postgres_dsn("not-a-dsn")

    def test_split_sql_keeps_semicolons_inside_quotes_and_comments(self) -> None:
        """Migration bodies split only at top-level statement boundaries."""
        script = """
        CREATE TABLE billing_core.example_row (
            example_code text CHECK (example_code IN ('a;b'))
        );
        -- trailing; comment
        INSERT INTO billing_core.example_row VALUES ('ok');
        /* block; comment */
        SELECT $tag$ keep; dollar $tag$;
        """
        statements = split_sql_statements(script)
        self.assertEqual(len(statements), 3)
        self.assertIn("'a;b'", statements[0])
        self.assertTrue(statements[1].startswith("INSERT"))
        self.assertTrue(statements[2].startswith("SELECT"))
        self.assertTrue(statements[2].endswith("$tag$"))

    def test_escaped_quotes_and_empty_script_branches(self) -> None:
        """Doubled quotes stay inside one statement; empty SQL yields no statements."""
        self.assertEqual(
            split_sql_statements("SELECT 'it''s; fine'; SELECT 2;"),
            ("SELECT 'it''s; fine'", "SELECT 2"),
        )
        self.assertEqual(
            split_sql_statements('SELECT "col;name", $$keep; dollar$$; SELECT 3;'),
            ('SELECT "col;name", $$keep; dollar$$', "SELECT 3"),
        )
        self.assertEqual(
            split_sql_statements('SELECT "quote"";inside"; SELECT 4;'),
            ('SELECT "quote"";inside"', "SELECT 4"),
        )
        self.assertEqual(split_sql_statements("   ;  ;"), ())

    def test_execute_runs_parameterized_and_multi_statement_sql(self) -> None:
        """Parameterized calls stay one statement; scripts run in order."""
        raw = FakeConnection()
        connection = PostgresConnection(raw)
        row = connection.execute(
            "SELECT count(*) FROM billing_core.usage_event WHERE source_event_key = %s",
            ("concurrent:source:01",),
        ).fetchone()
        self.assertEqual(row, (1,))
        self.assertEqual(
            raw.queries,
            [
                (
                    "SELECT count(*) FROM billing_core.usage_event WHERE source_event_key = %s",
                    ("concurrent:source:01",),
                )
            ],
        )
        raw.queries.clear()
        result = connection.execute("SELECT 1; SELECT 2;")
        self.assertEqual([sql for sql, _params in raw.queries], ["SELECT 1", "SELECT 2"])
        self.assertEqual(result.fetchall(), [(1,)])
        self.assertTrue(raw._cursors[1].closed)
        self.assertFalse(raw._cursors[2].closed)

    def test_execute_rejects_an_empty_script(self) -> None:
        """A script with no statements fails closed before opening a cursor."""
        connection = PostgresConnection(FakeConnection())
        with self.assertRaisesRegex(ValueError, "no executable statements"):
            connection.execute("   ;  ")

    def test_cursor_and_connection_context_managers_close(self) -> None:
        """Cursor and connection blocks close resources on both paths."""
        raw = FakeConnection()
        connection = PostgresConnection(raw)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))
        self.assertTrue(raw._cursors[0].closed)
        with PostgresConnection(raw) as wrapped:
            self.assertIs(wrapped._raw_connection, raw)
        self.assertEqual(raw.commits, 1)
        self.assertTrue(raw.closed)

        failing = FakeConnection()
        with self.assertRaises(RuntimeError):
            with PostgresConnection(failing):
                raise RuntimeError("boom")
        self.assertEqual(failing.rollbacks, 1)
        self.assertTrue(failing.closed)

    def test_transaction_commits_rolls_back_and_nests_savepoints(self) -> None:
        """Outer transactions commit or roll back; nested callers use savepoints."""
        raw = FakeConnection()
        connection = PostgresConnection(raw)
        with connection.transaction():
            connection.execute("SELECT 1")
        self.assertEqual(raw.commits, 1)
        self.assertEqual(raw.rollbacks, 0)

        rolling = FakeConnection()
        connection = PostgresConnection(rolling)
        with self.assertRaises(RuntimeError):
            with connection.transaction():
                raise RuntimeError("failed unit of work")
        self.assertEqual(rolling.rollbacks, 1)

        nested = FakeConnection()
        connection = PostgresConnection(nested)
        with connection.transaction():
            with connection.transaction():
                connection.execute("SELECT inner_work")
        savepoints = [sql for sql, _params in nested.queries]
        self.assertEqual(savepoints[0], "SAVEPOINT metering_billing_sp_2")
        self.assertEqual(savepoints[-1], "RELEASE SAVEPOINT metering_billing_sp_2")
        self.assertEqual(nested.commits, 1)

        nested_fail = FakeConnection()
        connection = PostgresConnection(nested_fail)
        with self.assertRaises(RuntimeError):
            with connection.transaction():
                with connection.transaction():
                    raise RuntimeError("nested failure")
        self.assertIn(
            "ROLLBACK TO SAVEPOINT metering_billing_sp_2",
            [sql for sql, _params in nested_fail.queries],
        )
        self.assertEqual(nested_fail.rollbacks, 1)

    def test_foreign_key_errors_are_translated(self) -> None:
        """SQLSTATE 23503 and foreign-key text become ForeignKeyViolation."""
        self.assertTrue(is_foreign_key_violation(ForeignKeyError("x", sqlstate="23503")))
        self.assertTrue(
            is_foreign_key_violation(ForeignKeyError("insert violates foreign key"))
        )
        self.assertTrue(is_foreign_key_violation(ForeignKeyError("error 23503")))
        self.assertFalse(is_foreign_key_violation(ForeignKeyError("syntax error")))
        with self.assertRaises(ForeignKeyViolation):
            raise_translated_database_error(ForeignKeyError("fk", sqlstate="23503"))
        with self.assertRaises(ForeignKeyError):
            raise_translated_database_error(ForeignKeyError("syntax error"))

        raw = FakeConnection()
        raw.execute_error = ForeignKeyError("violates foreign key constraint")
        connection = PostgresConnection(raw)
        with self.assertRaises(ForeignKeyViolation):
            connection.execute("INSERT INTO billing_core.usage_measurement VALUES (1)")

    def test_commit_rollback_and_connect_use_the_commercial_driver(self) -> None:
        """Explicit commit/rollback reach the raw session; connect wraps pg8000."""
        raw = FakeConnection()
        connection = PostgresConnection(raw)
        connection.commit()
        connection.rollback()
        self.assertEqual(raw.commits, 1)
        self.assertEqual(raw.rollbacks, 1)

        sentinel = FakeConnection()
        with mock.patch(
            "metering_billing.postgres_client.parse_postgres_dsn",
            return_value={"user": "postgres", "database": "metering_billing_test"},
        ), mock.patch(
            "metering_billing.postgres_client.pg8000.dbapi.connect",
            return_value=sentinel,
        ) as connect_driver:
            opened = connect("dbname=metering_billing_test user=postgres")
        connect_driver.assert_called_once_with(
            user="postgres", database="metering_billing_test"
        )
        self.assertIs(opened._raw_connection, sentinel)

    def test_cursor_context_manager_closes_after_direct_use(self) -> None:
        """The cursor wrapper itself is a context manager."""
        raw = FakeConnection()
        cursor = PostgresCursor(raw.cursor())
        with cursor as same:
            same.execute("SELECT 1")
        self.assertIs(same, cursor)
        self.assertTrue(raw._cursors[0].closed)


if __name__ == "__main__":
    unittest.main()
