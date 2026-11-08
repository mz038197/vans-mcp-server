"""One-off move of mcp_usage and mcp_oauth_connections from neondb to vans_mcp_server (#100).

Router ADR 0069, 0070, 0071, 0073. Rows are copied as they are: same ids, same
`user_id`, same ciphertext, so `OAUTH_TOKEN_ENCRYPTION_KEY` stays as it is and no
student authorizes again. Runbook: docs/mcp-database-move.md.

    uv run python -m vans_mcp_server.table_move status|copy|pause|final-copy|resume

Settings (read from the environment, never printed):

- VANS_MCP_MOVE_SOURCE_URL: the router's neondb, as the owner of the two tables
- VANS_MCP_DATABASE_URL: vans_mcp_server, as vans_mcp_server_app

Delete this module once the move is done and the tables are dropped from neondb.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from vans_mcp_server.database import SETTING as TARGET_SETTING
from vans_mcp_server.database import normalize_database_url
from vans_mcp_server.oauth.store import create_connections_table
from vans_mcp_server.usage import UsageLogger

SOURCE_SETTING = "VANS_MCP_MOVE_SOURCE_URL"

COLUMNS: dict[str, tuple[str, ...]] = {
    "mcp_usage": (
        "id",
        "user_id",
        "api_key_id",
        "tool_name",
        "success",
        "latency_ms",
        "error_type",
        "created_at",
    ),
    "mcp_oauth_connections": (
        "id",
        "user_id",
        "provider",
        "refresh_token_enc",
        "access_token_enc",
        "expires_at",
        "scopes",
        "google_sub",
        "created_at",
        "updated_at",
    ),
}
TABLES = tuple(COLUMNS)
PAUSE_TRIGGER = "vans_mcp_move_pause"
PAUSE_FUNCTION = "vans_mcp_move_refuse_write"


class MoveError(RuntimeError):
    """The move cannot go ahead safely; nothing was written."""


@dataclass
class TableSummary:
    source_rows: int = 0
    target_rows: int = 0
    copied: int = 0
    matches: bool = False


@dataclass
class Summary:
    tables: dict[str, TableSummary] = field(default_factory=dict)
    source_paused: bool = False
    seconds: float = 0.0

    @property
    def all_match(self) -> bool:
        return all(t.matches for t in self.tables.values())


# --- helpers ------------------------------------------------------------------------


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(normalize_database_url(url))


def _connect_snapshot(url: str) -> psycopg.Connection:
    """Read-only, one snapshot for the whole transaction."""
    conn = _connect(url)
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def _cols(table: str) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS[table])


def _table_columns(conn: psycopg.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return tuple(r[0] for r in rows)


def _check_source(conn: psycopg.Connection) -> None:
    for table in TABLES:
        found = _table_columns(conn, table)
        if not found:
            raise MoveError(f"source has no table {table}")
        if set(found) != set(COLUMNS[table]):
            raise MoveError(
                f"source {table} columns {sorted(found)} differ from {sorted(COLUMNS[table])}"
            )


def _check_different_databases(source: psycopg.Connection, target: psycopg.Connection) -> None:
    query = "SELECT current_database(), current_setting('port'), inet_server_addr()::text"
    if source.execute(query).fetchone() == target.execute(query).fetchone():
        raise MoveError("source and target are the same database")


def _ids(conn: psycopg.Connection, table: str) -> set[int]:
    query = sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
    return {r[0] for r in conn.execute(query).fetchall()}


def _fingerprint(conn: psycopg.Connection, table: str) -> tuple[int, str | None]:
    """Row count and an md5 over every row in id order; compares data without printing it."""
    query = sql.SQL(
        "SELECT count(*), md5(string_agg(row({cols})::text, E'\\n' ORDER BY id)) FROM {table}"
    ).format(cols=_cols(table), table=sql.Identifier(table))
    count, digest = conn.execute(query).fetchone()
    return int(count), digest


def _stream(
    source: psycopg.Connection,
    target: psycopg.Connection,
    table: str,
    ids: list[int] | None,
) -> None:
    """COPY rows source -> target; COPY keeps the identity values (no new ids)."""
    if ids is None:
        out = sql.SQL("COPY (SELECT {cols} FROM {table} ORDER BY id) TO STDOUT").format(
            cols=_cols(table), table=sql.Identifier(table)
        )
    else:
        out = sql.SQL(
            "COPY (SELECT {cols} FROM {table} WHERE id = ANY({ids}) ORDER BY id) TO STDOUT"
        ).format(cols=_cols(table), table=sql.Identifier(table), ids=sql.Literal(ids))
    into = sql.SQL("COPY {table} ({cols}) FROM STDIN").format(
        table=sql.Identifier(table), cols=_cols(table)
    )
    with source.cursor() as scur, target.cursor() as tcur:
        with scur.copy(out) as reader, tcur.copy(into) as writer:
            for chunk in reader:
                writer.write(chunk)


def _bump_identity(target: psycopg.Connection, table: str) -> None:
    target.execute(
        sql.SQL(
            "SELECT setval(pg_get_serial_sequence({name}, 'id'), "
            "COALESCE((SELECT max(id) FROM {table}), 1), "
            "(SELECT max(id) FROM {table}) IS NOT NULL)"
        ).format(name=sql.Literal(table), table=sql.Identifier(table))
    )


def _updated_at(conn: psycopg.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT id, updated_at FROM mcp_oauth_connections").fetchall()
    return {r[0]: r[1] for r in rows}


def _guard_target(source: psycopg.Connection, target: psycopg.Connection) -> None:
    """Refuse when the target holds rows the source does not: the service already switched."""
    extra = _ids(target, "mcp_usage") - _ids(source, "mcp_usage")
    if extra:
        raise MoveError(
            f"target mcp_usage has {len(extra)} rows the source does not; "
            "has the service already switched? Nothing was copied."
        )
    source_updated = _updated_at(source)
    for row_id, updated in _updated_at(target).items():
        if row_id not in source_updated or updated > source_updated[row_id]:
            raise MoveError(
                "target mcp_oauth_connections has rows newer than the source; "
                "has the service already switched? Nothing was copied."
            )


def _source_paused(source: psycopg.Connection) -> bool:
    """True when both tables carry the enabled pause trigger."""
    paused = source.execute(
        """
        SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relnamespace = 'public'::regnamespace AND c.relname = ANY(%s)
          AND t.tgname = %s AND t.tgenabled <> 'D'
        """,
        (list(TABLES), PAUSE_TRIGGER),
    ).fetchone()[0]
    return paused == len(TABLES)


def _summarize(source: psycopg.Connection, target: psycopg.Connection, summary: Summary) -> None:
    for table in TABLES:
        s_count, s_digest = _fingerprint(source, table)
        if _table_columns(target, table):
            t_count, t_digest = _fingerprint(target, table)
        else:  # not created yet: copy creates it
            t_count, t_digest = 0, None
        entry = summary.tables.setdefault(table, TableSummary())
        entry.source_rows = s_count
        entry.target_rows = t_count
        entry.matches = s_count == t_count and s_digest == t_digest
    summary.source_paused = _source_paused(source)


# --- commands -----------------------------------------------------------------------


def copy(source_url: str, target_url: str) -> Summary:
    """Create the tables in the target (as its role) and copy what the source has.

    Safe to re-run: mcp_usage gets only the rows it is missing, mcp_oauth_connections is
    replaced with the source's rows in one transaction. Refuses when the target already
    holds data the source lacks.
    """
    started = time.monotonic()
    target_url = normalize_database_url(target_url)
    UsageLogger(target_url)
    create_connections_table(target_url)
    summary = Summary()
    with _connect_snapshot(source_url) as source, _connect(target_url) as target:
        _check_source(source)
        _check_different_databases(source, target)
        _guard_target(source, target)

        missing = sorted(_ids(source, "mcp_usage") - _ids(target, "mcp_usage"))
        if missing:
            _stream(source, target, "mcp_usage", missing)
        _bump_identity(target, "mcp_usage")
        summary.tables["mcp_usage"] = TableSummary(copied=len(missing))

        target.execute("DELETE FROM mcp_oauth_connections")
        _stream(source, target, "mcp_oauth_connections", None)
        _bump_identity(target, "mcp_oauth_connections")
        target.commit()
        summary.tables["mcp_oauth_connections"] = TableSummary(
            copied=_fingerprint(target, "mcp_oauth_connections")[0]
        )
        _summarize(source, target, summary)
        source.rollback()
    summary.seconds = time.monotonic() - started
    return summary


def status(source_url: str, target_url: str) -> Summary:
    """Row counts, whether each table matches, and whether neondb is paused. Writes nothing."""
    summary = Summary()
    with _connect_snapshot(source_url) as source, _connect(target_url) as target:
        _check_source(source)
        _summarize(source, target, summary)
    return summary


def _owned_tables(conn: psycopg.Connection) -> None:
    for table in TABLES:
        row = conn.execute(
            "SELECT tableowner = current_user FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchone()
        if row is None:
            raise MoveError(f"source has no table {table}")
        if not row[0]:
            raise MoveError(f"connect to the source as the owner of {table}")




def pause(source_url: str) -> None:
    """Stop writes to both tables on neondb; reads keep working.

    A trigger refuses every INSERT, UPDATE, DELETE and TRUNCATE. Revoking privileges is
    not enough on Neon: neondb_owner is in neon_superuser, which holds pg_write_all_data.
    CREATE TRIGGER waits for any write already in flight. The router does not touch
    these tables.
    """
    with _connect(source_url) as source:
        _owned_tables(source)
        source.execute(
            sql.SQL(
                "CREATE OR REPLACE FUNCTION public.{fn}() RETURNS trigger LANGUAGE plpgsql AS $$"
                " BEGIN RAISE EXCEPTION 'writes paused for the vans_mcp_server move'"
                " USING ERRCODE = 'insufficient_privilege'; END $$"
            ).format(fn=sql.Identifier(PAUSE_FUNCTION))
        )
        for table in TABLES:
            source.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {trg} ON public.{t}").format(
                    trg=sql.Identifier(PAUSE_TRIGGER), t=sql.Identifier(table)
                )
            )
            source.execute(
                sql.SQL(
                    "CREATE TRIGGER {trg} BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE"
                    " ON public.{t} FOR EACH STATEMENT EXECUTE FUNCTION public.{fn}()"
                ).format(
                    trg=sql.Identifier(PAUSE_TRIGGER),
                    t=sql.Identifier(table),
                    fn=sql.Identifier(PAUSE_FUNCTION),
                )
            )
        source.commit()


def resume(source_url: str) -> None:
    """Undo pause: drop the trigger so writes work again (rollback before the switch)."""
    with _connect(source_url) as source:
        _owned_tables(source)
        for table in TABLES:
            source.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {trg} ON public.{t}").format(
                    trg=sql.Identifier(PAUSE_TRIGGER), t=sql.Identifier(table)
                )
            )
        source.execute(
            sql.SQL("DROP FUNCTION IF EXISTS public.{fn}()").format(fn=sql.Identifier(PAUSE_FUNCTION))
        )
        source.commit()


def final_copy(source_url: str, target_url: str) -> Summary:
    """Copy the gap while neondb is paused; end with an exact match or raise."""
    with _connect(source_url) as source:
        if not _source_paused(source):
            raise MoveError("neondb is not paused; run pause first")
    summary = copy(source_url, target_url)
    if not (summary.source_paused and summary.all_match):
        raise MoveError("target does not match the paused source; do not switch")
    return summary


# --- command line -------------------------------------------------------------------


def describe_url(url: str) -> str:
    """user@host/database, without the password or query string."""
    parts = urlsplit(normalize_database_url(url))
    return f"{parts.username or '?'}@{parts.hostname or '?'}{parts.path or ''}"


def render(summary: Summary) -> str:
    lines = [f"{'table':<24}{'source':>9}{'target':>9}{'copied':>9}  match"]
    for table, t in summary.tables.items():
        lines.append(
            f"{table:<24}{t.source_rows:>9}{t.target_rows:>9}{t.copied:>9}  "
            f"{'yes' if t.matches else 'NO'}"
        )
    lines.append(f"neondb paused: {'yes' if summary.source_paused else 'no'}")
    if summary.seconds:
        lines.append(f"took {summary.seconds:.1f} s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    commands = ("status", "copy", "pause", "final-copy", "resume")
    if len(args) != 1 or args[0] not in commands:
        print(f"usage: python -m vans_mcp_server.table_move {{{'|'.join(commands)}}}")
        return 2
    command = args[0]
    source_url = (os.environ.get(SOURCE_SETTING) or "").strip()
    target_url = (os.environ.get(TARGET_SETTING) or "").strip()
    if not source_url or not target_url:
        print(f"set {SOURCE_SETTING} and {TARGET_SETTING}")
        return 2
    print(f"source {describe_url(source_url)}  ->  target {describe_url(target_url)}")
    try:
        if command == "pause":
            pause(source_url)
            print("neondb paused: writes to mcp_usage and mcp_oauth_connections are refused")
            print(render(status(source_url, target_url)))
        elif command == "resume":
            resume(source_url)
            print("neondb resumed: the owner can write both tables again")
        elif command == "status":
            print(render(status(source_url, target_url)))
        elif command == "copy":
            print(render(copy(source_url, target_url)))
        else:
            print(render(final_copy(source_url, target_url)))
            print("READY: switch the service to VANS_MCP_DATABASE_URL")
    except (MoveError, psycopg.Error) as exc:
        # psycopg messages name the server and role, never the password.
        print(f"FAILED ({type(exc).__name__}): {str(exc).strip()}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
