"""Ordinary install/upgrade and bounded reservation concurrency contracts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from scripts.extension_identity import (
    ENQUEUE, candidate_version, installed_identity, source_enqueue_body,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_database(admin, dsn):
    database = "reservation_" + uuid.uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    options = conninfo_to_dict(dsn)
    options["dbname"] = database
    isolated_dsn = make_conninfo(**options)
    try:
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            yield conn, isolated_dsn
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def functions(conn):
    return conn.execute("""SELECT p.proname, pg_get_function_identity_arguments(p.oid),
        p.prosrc, p.prosecdef, p.proconfig, p.provolatile, p.proparallel,
        p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='echoo_pgmq' ORDER BY 1,2""").fetchall()


def retained_state(conn):
    return {table: conn.execute(sql.SQL("SELECT * FROM echoo_pgmq.{} ORDER BY 1,2").format(
        sql.Identifier(table))).fetchall()
        for table in ("limits", "queues", "queue_acl", "messages", "idempotency")}


def seed_retained_state(conn):
    conn.execute("SELECT echoo_pgmq.create_queue('retained')")
    conn.execute("SELECT echoo_pgmq.grant_queue('retained',session_user::text)")
    conn.execute("SELECT echoo_pgmq.grant_queue('retained','echoo_test_user')")
    # CREATE OR REPLACE must retain existing function ACLs as well as queue ACLs.
    conn.execute("GRANT EXECUTE ON FUNCTION echoo_pgmq._enqueue(text,bytea,text,text) TO echoo_test_user")
    body = b"\x00\xff\x80retained"
    ident = conn.execute("SELECT echoo_pgmq.enqueue('retained',%s,'retained-key')", (body,)).fetchone()[0]
    owner = uuid.uuid4()
    receipt = conn.execute("SELECT * FROM echoo_pgmq.read('retained',%s,3600)", (owner,)).fetchone()
    assert receipt[0] == ident and receipt[2] == body
    return ident, owner, receipt


def assert_retained_contract(conn, before, old_function, ident, owner, receipt):
    assert retained_state(conn) == before
    current = conn.execute("SELECT oid,proacl::text FROM pg_proc WHERE oid=%s::regprocedure", (ENQUEUE,)).fetchone()
    assert current == old_function
    assert installed_identity(conn)["extversion"] == "0.1.2"
    # A retained key still deduplicates without changing any ledger or receipt.
    assert conn.execute("SELECT echoo_pgmq.enqueue('retained',%s,'retained-key')", (b"ignored",)).fetchone()[0] == ident
    assert retained_state(conn) == before
    assert conn.execute("SELECT echoo_pgmq.ack('retained',%s,%s,%s)", (ident, receipt[1], owner)).fetchone()[0]
    assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (0, 0)
    assert conn.execute("SELECT message_count,total_bytes,idempotency_keys FROM echoo_pgmq.queues").fetchone() == (0, 0, 1)


def test_current_install_and_source_identity(admin):
    assert candidate_version() == "0.1.2"
    identity = installed_identity(admin)
    assert identity["extversion"] == "0.1.2"
    assert source_enqueue_body("0.1.2") == source_enqueue_body("0.1.2", migration=True)
    root = Path(__file__).resolve().parents[1]
    assert "project(echoo_pgmq VERSION 0.1.2 " in (root / "CMakeLists.txt").read_text()
    assert not admin.execute("SELECT has_function_privilege('echoo_denied',%s,'EXECUTE')", (ENQUEUE,)).fetchone()[0]


@pytest.mark.parametrize("start_version", ["0.1.0", "0.1.1"])
def test_upgrade_preserves_contract_and_retained_state(isolated_database, admin, start_version):
    conn, _ = isolated_database
    conn.execute(sql.SQL("CREATE EXTENSION echoo_pgmq VERSION {}").format(sql.Literal(start_version)))
    ident, owner, receipt = seed_retained_state(conn)
    before = retained_state(conn)
    old_acls = [row[:2] + row[-1:] for row in functions(conn)]
    old_function = conn.execute("SELECT oid,proacl::text FROM pg_proc WHERE oid=%s::regprocedure", (ENQUEUE,)).fetchone()
    if start_version == "0.1.0":
        conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.1'")
        assert retained_state(conn) == before
    conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'")
    # Fresh install and sequential migration have identical function semantics;
    # the only ACL difference is the administrator grant deliberately retained.
    upgraded = functions(conn)
    fresh = functions(admin)
    assert [row[:-1] for row in upgraded] == [row[:-1] for row in fresh]
    assert [row[:2] + row[-1:] for row in upgraded] == old_acls
    assert conn.execute("SELECT pg_get_viewdef('echoo_pgmq.queue_stats'::regclass)").fetchone() == admin.execute(
        "SELECT pg_get_viewdef('echoo_pgmq.queue_stats'::regclass)").fetchone()
    assert not conn.execute("SELECT has_function_privilege('echoo_denied',%s,'EXECUTE')", (ENQUEUE,)).fetchone()[0]
    assert_retained_contract(conn, before, old_function, ident, owner, receipt)


def test_explicit_upgrade_rollback_then_retry(isolated_database):
    conn, _ = isolated_database
    conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'")
    ident, owner, receipt = seed_retained_state(conn)
    before = retained_state(conn)
    old_function = conn.execute("SELECT oid,proacl::text FROM pg_proc WHERE oid=%s::regprocedure", (ENQUEUE,)).fetchone()
    old_identity = installed_identity(conn, "0.1.1")
    old_functions = functions(conn)
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'")
        assert installed_identity(conn)["enqueue_prosrc_sha256"] != old_identity["enqueue_prosrc_sha256"]
    finally:
        conn.execute("ROLLBACK")
    assert installed_identity(conn, "0.1.1") == old_identity
    assert functions(conn) == old_functions
    assert retained_state(conn) == before
    conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'")
    assert_retained_contract(conn, before, old_function, ident, owner, receipt)


def prepare_queue(conn, name="wait", max_messages=1):
    conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.2'")
    conn.execute("SELECT echoo_pgmq.create_queue(%s,%s,1024,1024,5)", (name, max_messages))
    conn.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (name,))


def assert_ledger(conn, count, size):
    rows = conn.execute("""SELECT q.message_count,q.total_bytes,count(m.id),
        coalesce(sum(octet_length(m.body)),0) FROM echoo_pgmq.queues q
        LEFT JOIN echoo_pgmq.messages m USING(queue_id) GROUP BY q.queue_id""").fetchall()
    assert all((n, b) == (actual, actual_bytes) for n, b, actual, actual_bytes in rows)
    assert (sum(row[0] for row in rows), sum(row[1] for row in rows)) == (count, size)
    assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (count, size)


def blocked_enqueue(observer, dsn, holder, commit):
    with psycopg.connect(dsn, autocommit=True) as producer:
        producer.execute("SET statement_timeout='8s'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: producer.execute("SELECT echoo_pgmq.enqueue('wait',%s)", (b"new",)).fetchone()[0])
            try:
                deadline = time.monotonic() + 5
                witnessed = False
                while time.monotonic() < deadline:
                    assert not future.done(), "enqueue did not wait for the pending release/configuration change"
                    row = observer.execute("""SELECT wait_event_type,wait_event,
                        %s=ANY(pg_blocking_pids(pid)) FROM pg_stat_activity WHERE pid=%s""",
                        (holder.info.backend_pid, producer.info.backend_pid)).fetchone()
                    if row == ("Lock", "transactionid", True):
                        witnessed = True
                        break
                    time.sleep(.01)
                assert witnessed, "no transaction-ID wait on the intended holder"
                if commit:
                    holder.commit()
                    return future.result(timeout=8)
                holder.rollback()
                with pytest.raises(psycopg.errors.ProgramLimitExceeded):
                    future.result(timeout=8)
                return None
            finally:
                # Never leave the executor waiting on a lock held by this test.
                holder.rollback()


@pytest.mark.parametrize("commit", [True, False])
def test_full_global_waits_for_pending_release(isolated_database, commit):
    conn, dsn = isolated_database
    prepare_queue(conn, max_messages=2)
    conn.execute("UPDATE echoo_pgmq.limits SET max_messages=1")
    ident = conn.execute("SELECT echoo_pgmq.enqueue('wait',%s)", (b"old",)).fetchone()[0]
    owner = uuid.uuid4()
    receipt = conn.execute("SELECT * FROM echoo_pgmq.read('wait',%s,60)", (owner,)).fetchone()
    with psycopg.connect(dsn) as holder:
        assert holder.execute("SELECT echoo_pgmq.ack('wait',%s,%s,%s)", (ident, receipt[1], owner)).fetchone()[0]
        new = blocked_enqueue(conn, dsn, holder, commit)
    assert_ledger(conn, 1, 3)
    assert conn.execute("SELECT id,body FROM echoo_pgmq.messages").fetchone() == ((new, b"new") if commit else (ident, b"old"))


@pytest.mark.parametrize("commit", [True, False])
def test_full_queue_waits_for_pending_capacity_increase(isolated_database, commit):
    conn, dsn = isolated_database
    prepare_queue(conn)
    conn.execute("SELECT echoo_pgmq.enqueue('wait',%s)", (b"old",))
    with psycopg.connect(dsn) as holder:
        holder.execute("UPDATE echoo_pgmq.queues SET max_messages=2 WHERE name='wait'")
        new = blocked_enqueue(conn, dsn, holder, commit)
    assert (new is not None) == commit
    assert_ledger(conn, 2 if commit else 1, 6 if commit else 3)


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_cross_queue_global_quota_cannot_overbook(isolated_database, bound):
    conn, dsn = isolated_database
    conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.2'")
    for index in range(6):
        conn.execute("SELECT echoo_pgmq.create_queue(%s)", (f"race{index}",))
        conn.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (f"race{index}",))
    # create_queue seeds the singleton; prove the configured cap was applied.
    assert conn.execute("UPDATE echoo_pgmq.limits SET max_messages=%s,max_bytes=%s",
                        (3 if bound == "count" else 100, 1024 if bound == "count" else 9)).rowcount == 1
    barrier = threading.Barrier(6)
    def produce(index):
        with psycopg.connect(dsn, autocommit=True) as other:
            other.execute("SET statement_timeout='8s'")
            barrier.wait(timeout=5)
            try:
                return other.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (f"race{index}", b"abc")).fetchone()[0]
            except psycopg.errors.ProgramLimitExceeded:
                return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(produce, range(6)))
    accepted = [ident for ident in results if ident is not None]
    assert len(accepted) == len(set(accepted)) == 3
    assert_ledger(conn, 3, 9)
