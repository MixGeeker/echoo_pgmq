"""Hard storage limits, concurrent idempotency, and logical backup round trips."""
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import threading
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

pytestmark = pytest.mark.integration


def parallel(count, action):
    barrier = threading.Barrier(count)
    def run(index):
        barrier.wait(timeout=20)
        return action(index)
    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def test_concurrent_same_key_creates_one_message(admin, user_dsn, queue):
    def produce(_):
        with psycopg.connect(user_dsn, autocommit=True) as conn:
            return conn.execute("SELECT echoo_pgmq.enqueue(%s,%s,'same-key')",
                                (queue, b"\x00\xffsame-key")).fetchone()[0]
    ids = parallel(12, produce)
    assert len(set(ids)) == 1
    assert admin.execute("SELECT message_count,total_bytes,idempotency_keys FROM echoo_pgmq.queues WHERE name=%s",
                         (queue,)).fetchone() == (1, 10, 1)


def test_concurrent_key_quota_cannot_overbook(admin, user_dsn, queue):
    admin.execute("UPDATE echoo_pgmq.queues SET max_idempotency_keys=2 WHERE name=%s", (queue,))
    def produce(index):
        with psycopg.connect(user_dsn, autocommit=True) as conn:
            try:
                conn.execute("SELECT echoo_pgmq.enqueue(%s,%s,%s)", (queue, b"key", str(index)))
                return True
            except psycopg.errors.ProgramLimitExceeded:
                return False
    results = parallel(12, produce)
    assert sum(results) == 2
    assert admin.execute("SELECT message_count,total_bytes,idempotency_keys FROM echoo_pgmq.queues WHERE name=%s",
                         (queue,)).fetchone() == (2, 6, 2)
    assert admin.execute("SELECT count(*) FROM echoo_pgmq.idempotency d JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s",
                         (queue,)).fetchone()[0] == 2


def test_rollback_releases_message_bytes_and_key_budget(admin, user_dsn, queue):
    before = admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone()
    with psycopg.connect(user_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):
            with conn.transaction():
                conn.execute("SELECT echoo_pgmq.enqueue(%s,%s,'rollback-key')", (queue, b"rollback"))
                conn.execute("SELECT 1/0")
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == before
    assert admin.execute("SELECT message_count,total_bytes,idempotency_keys FROM echoo_pgmq.queues WHERE name=%s",
                         (queue,)).fetchone() == (0, 0, 0)
    assert admin.execute("SELECT count(*) FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s",
                         (queue,)).fetchone()[0] == 0


def test_global_limits_apply_across_queues_and_rollback(admin, client, queue):
    before = admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone()
    limits = admin.execute("SELECT max_messages,max_bytes FROM echoo_pgmq.limits").fetchone()
    other_queue = "global_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s)", (other_queue,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (other_queue,))
    # Own a transaction and alter global limits only inside it, avoiding leaked
    # configuration if a test fails. Use the admin identity with an explicit ACL.
    with admin.transaction():
        admin.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (queue,))
        admin.execute("UPDATE echoo_pgmq.limits SET max_messages=message_count+1,max_bytes=total_bytes+3")
        admin.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"abc"))
        with pytest.raises(psycopg.errors.ProgramLimitExceeded):
            with admin.transaction():
                admin.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (other_queue, b"d"))
        admin.execute("UPDATE echoo_pgmq.limits SET max_messages=%s,max_bytes=%s", limits)
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (before[0]+1,before[1]+3)


def test_logical_dump_restores_payload_acl_ledger_receipts_and_sequences(dsn, admin, tmp_path):
    pg_dump, psql = shutil.which("pg_dump"), shutil.which("psql")
    assert pg_dump and psql, "PostgreSQL bin directory must be on PATH"
    suffix = uuid.uuid4().hex
    source, restored = "dump_" + suffix, "restore_" + suffix
    options = conninfo_to_dict(dsn)
    for database in (source, restored):
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    owner = uuid.uuid4()
    backup_principal = "backup_role_" + suffix[:20]
    admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(backup_principal)))
    try:
        options["dbname"] = source
        source_dsn = make_conninfo(**options)
        with psycopg.connect(source_dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'")
            for queue_name in ("backup/one", "backup/two", "backup/three"):
                conn.execute("SELECT echoo_pgmq.create_queue(%s)", (queue_name,))
                conn.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (queue_name,))
            conn.execute("SELECT echoo_pgmq.grant_queue('backup/one',%s)", (backup_principal,))
            first = conn.execute("SELECT echoo_pgmq.enqueue('backup/one',%s,'backup-key')", (b"\x00\xff\x80first",)).fetchone()[0]
            receipt = conn.execute("SELECT * FROM echoo_pgmq.read('backup/one',%s,3600)", (owner,)).fetchone()
            conn.execute("SELECT echoo_pgmq.enqueue('backup/two',%s)", (b"second",))
            conn.execute("SELECT echoo_pgmq.enqueue('backup/three',%s)", (b"third",))
            highest = conn.execute("SELECT max(id) FROM echoo_pgmq.messages").fetchone()[0]
            counters = conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone()
            assert first == receipt[0]
        dump = tmp_path / "echoo.sql"
        subprocess.run([pg_dump, "--dbname", source_dsn, "--file", str(dump)], check=True, capture_output=True, text=True)
        # Simulate a target cluster resolving the same role name to a new OID.
        old_role = admin.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (backup_principal,)).fetchone()[0]
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(backup_principal)))
        admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(backup_principal)))
        assert admin.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (backup_principal,)).fetchone()[0] != old_role
        options["dbname"] = restored
        restore_dsn = make_conninfo(**options)
        # pg_dump omits the extension version from CREATE EXTENSION. Preserve
        # the source API contract explicitly, not merely its table contents.
        with psycopg.connect(restore_dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'")
        subprocess.run([psql, "--dbname", restore_dsn, "-X", "-v", "ON_ERROR_STOP=1", "--file", str(dump)],
                       check=True, capture_output=True, text=True)
        with psycopg.connect(restore_dsn, autocommit=True) as conn:
            assert conn.execute("SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq'").fetchone()[0] == "0.1.1"
            assert conn.execute("SELECT to_regclass('echoo_pgmq.queue_stats')").fetchone()[0] is not None
            assert conn.execute("SELECT count(*) FROM echoo_pgmq.queue_stats").fetchone()[0] == 3
            assert conn.execute("SELECT echoo_pgmq.authorize('backup/one',%s,'consume')", (backup_principal,)).fetchone()[0]
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == counters
            assert conn.execute("SELECT echoo_pgmq.enqueue('backup/one',%s,'backup-key')", (b"ignored",)).fetchone()[0] == first
            assert conn.execute("SELECT body,generation,owner FROM echoo_pgmq.messages WHERE id=%s", (first,)).fetchone() == (b"\x00\xff\x80first", receipt[1], owner)
            assert conn.execute("SELECT echoo_pgmq.ack('backup/one',%s,%s,%s)", (first,receipt[1],owner)).fetchone()[0]
            assert conn.execute("SELECT echoo_pgmq.enqueue('backup/two',%s)", (b"new",)).fetchone()[0] > highest
            conn.execute("SELECT echoo_pgmq.create_queue('backup/four')")
            assert conn.execute("SELECT queue_id FROM echoo_pgmq.queues WHERE name='backup/four'").fetchone()[0] > 3
    finally:
        for database in (source, restored):
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(backup_principal)))


def test_binary_helper_preserves_application_bytes(client, queue):
    from proton import Message
    payload = bytes(range(256)) + b"\x00\xff\x80"
    encoded = client.execute("SELECT echoo_pgmq.message_binary(%s)", (payload,)).fetchone()[0]
    message = Message()
    message.decode(encoded)
    assert bytes(message.body) == payload
    assert message.durable
    assert message.content_type == "application/octet-stream"
    ident = client.execute("SELECT echoo_pgmq.enqueue_binary(%s,%s)", (queue, payload)).fetchone()[0]
    receipt = client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue, uuid.uuid4())).fetchone()
    assert receipt[0] == ident
    assert receipt[2] == encoded


def test_expired_key_reuses_bounded_slot(admin, client, queue):
    admin.execute("UPDATE echoo_pgmq.queues SET max_idempotency_keys=1 WHERE name=%s", (queue,))
    first = client.execute("SELECT echoo_pgmq.enqueue(%s,%s,'expiring')", (queue, b"one")).fetchone()[0]
    admin.execute("UPDATE echoo_pgmq.idempotency d SET expires_at=clock_timestamp()-interval '1 second' "
                  "FROM echoo_pgmq.queues q WHERE d.queue_id=q.queue_id AND q.name=%s", (queue,))
    second = client.execute("SELECT echoo_pgmq.enqueue(%s,%s,'expiring')", (queue, b"two")).fetchone()[0]
    assert second != first
    assert admin.execute("SELECT idempotency_keys,message_count FROM echoo_pgmq.queues WHERE name=%s",
                         (queue,)).fetchone() == (1, 2)


def test_dropped_or_recreated_role_does_not_inherit_queue_acl(admin, queue):
    principal = "ephemeral_" + uuid.uuid4().hex[:20]
    admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(principal)))
    try:
        admin.execute("SELECT echoo_pgmq.grant_queue(%s,%s)", (queue, principal))
        assert admin.execute("SELECT echoo_pgmq.authorize(%s,%s,'publish')", (queue,principal)).fetchone()[0]
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(principal)))
        assert not admin.execute("SELECT echoo_pgmq.authorize(%s,%s,'publish')", (queue,principal)).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("SELECT echoo_pgmq.publish(%s,%s,%s)", (queue,b"denied",principal))
        admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(principal)))
        assert not admin.execute("SELECT echoo_pgmq.authorize(%s,%s,'publish')", (queue,principal)).fetchone()[0]
        admin.execute("SELECT echoo_pgmq.grant_queue(%s,%s)", (queue,principal))
        assert admin.execute("SELECT echoo_pgmq.authorize(%s,%s,'publish')", (queue,principal)).fetchone()[0]
    finally:
        admin.execute("SELECT echoo_pgmq.revoke_queue(%s,%s)", (queue,principal))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(principal)))


def test_administrative_retry_invalidates_old_receipt(admin, client, queue):
    owner = uuid.uuid4()
    ident = client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"retry-body")).fetchone()[0]
    first = client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue, owner)).fetchone()
    assert client.execute("SELECT echoo_pgmq.reject(%s,%s,%s,%s)", (queue,ident,first[1],owner)).fetchone()[0]
    assert admin.execute("SELECT echoo_pgmq.retry_dead(%s,%s)", (queue,ident)).fetchone()[0]
    current = client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue,owner)).fetchone()
    assert current[1] > first[1]
    assert current[2] == b"retry-body"
    assert not client.execute("SELECT echoo_pgmq.ack(%s,%s,%s,%s)", (queue,ident,first[1],owner)).fetchone()[0]
    assert client.execute("SELECT echoo_pgmq.ack(%s,%s,%s,%s)", (queue,ident,current[1],owner)).fetchone()[0]


def test_expired_final_attempt_cleanup_is_bounded(admin, client, queue):
    admin.execute("UPDATE echoo_pgmq.queues SET max_attempts=1 WHERE name=%s", (queue,))
    owner = uuid.uuid4()
    for index in range(70):
        client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, bytes([index])))
        assert client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,3600)", (queue,owner)).fetchone()
    assert client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue,owner)).fetchone() is None
    # Advance lease timestamps without a long test sleep; receipt-expiration by
    # actual elapsed time is separately asserted by tests/sql/core.sql.
    admin.execute("UPDATE echoo_pgmq.messages m SET available_at=clock_timestamp()-interval '1 second',"
                  "lease_until=clock_timestamp()-interval '1 second' FROM echoo_pgmq.queues q "
                  "WHERE m.queue_id=q.queue_id AND q.name=%s", (queue,))
    assert client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue,owner)).fetchone() is None
    count_dead = "SELECT count(*) FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s AND m.state='dead'"
    assert admin.execute(count_dead, (queue,)).fetchone()[0] == 64
    assert client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue,owner)).fetchone() is None
    assert admin.execute(count_dead, (queue,)).fetchone()[0] == 70
    assert admin.execute("SELECT echoo_pgmq.purge_dead(%s,17)", (queue,)).fetchone()[0] == 17
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone() == (53,53)


def test_poison_quarantine_does_not_deadlock_with_late_ack(admin, client, user_dsn, queue):
    """A claim owns the message row while an old ACK owns the quota lock."""
    import time
    old_owner, new_owner = uuid.uuid4(), uuid.uuid4()
    ident = client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"")).fetchone()[0]
    old = client.execute("SELECT * FROM echoo_pgmq.read(%s,%s,60)", (queue, old_owner)).fetchone()
    admin.execute("UPDATE echoo_pgmq.messages SET lease_until=clock_timestamp()-interval '1 second',"
                  "available_at=clock_timestamp()-interval '1 second' WHERE id=%s", (ident,))
    with psycopg.connect(user_dsn) as claimant, psycopg.connect(user_dsn, autocommit=True) as stale:
        new = claimant.execute("SELECT * FROM echoo_pgmq.read(%s,%s,60)", (queue,new_owner)).fetchone()
        stale_pid = stale.info.backend_pid
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: stale.execute("SELECT echoo_pgmq.ack(%s,%s,%s,%s)",
                                     (queue,ident,old[1],old_owner)).fetchone()[0])
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                blocked = admin.execute("SELECT %s=ANY(pg_blocking_pids(%s))",
                                        (claimant.info.backend_pid,stale_pid)).fetchone()[0]
                if blocked:
                    break
                time.sleep(.01)
            assert blocked, "late ACK did not reach the intended row-lock boundary"
            # A two-second timeout makes a reversed-lock regression deterministic,
            # rather than hanging the suite until a PostgreSQL deadlock is chosen.
            claimant.execute("SET LOCAL statement_timeout='2s'")
            assert claimant.execute("SELECT echoo_pgmq.reject(%s,%s,%s,%s)",
                                    (queue,ident,new[1],new_owner)).fetchone()[0]
            claimant.commit()
            assert future.result(timeout=5) is False
    assert admin.execute("SELECT state FROM echoo_pgmq.messages WHERE id=%s", (ident,)).fetchone()[0] == "dead"
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone() == (1,0)
