"""Concurrency, real session-role ACL, and transactional upgrade contract."""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

pytestmark = pytest.mark.integration


def test_concurrent_claims_are_unique(admin, user_dsn, queue):
    with psycopg.connect(user_dsn, autocommit=True) as conn:
        ids = {conn.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, bytes([i]))).fetchone()[0]
               for i in range(24)}
    barrier = threading.Barrier(6)
    def consume(_):
        got = []
        with psycopg.connect(user_dsn, autocommit=True) as conn:
            owner = uuid.uuid4()
            barrier.wait(timeout=10)
            while row := conn.execute("SELECT * FROM echoo_pgmq.read(%s,%s,30)", (queue, owner)).fetchone():
                got.append(row[0])
                assert conn.execute("SELECT echoo_pgmq.ack(%s,%s,%s,%s)",
                                    (queue, row[0], row[1], owner)).fetchone()[0]
        return got
    with ThreadPoolExecutor(max_workers=6) as executor:
        received = [message for group in executor.map(consume, range(6)) for message in group]
    assert len(received) == len(set(received)) == 24
    assert set(received) == ids
    assert admin.execute("SELECT message_count FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone()[0] == 0


def test_concurrent_capacity_cannot_overbook(admin, user_dsn):
    queue = "capacity_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s,1,1024,1024,5)", (queue,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s,'echoo_test_user')", (queue,))
    barrier = threading.Barrier(8)
    def produce(_):
        with psycopg.connect(user_dsn, autocommit=True) as conn:
            barrier.wait(timeout=10)
            try:
                conn.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"payload"))
                return "accepted"
            except psycopg.Error as error:
                # Capacity is SQLSTATE 54000 (program_limit_exceeded), not any error.
                assert error.sqlstate == "54000", error
                return "limited"
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(produce, range(8)))
    assert results.count("accepted") == 1
    assert results.count("limited") == 7
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone() == (1, 7)


def test_ordinary_role_cannot_call_private_worker_api(dsn, queue):
    opts = conninfo_to_dict(dsn)
    opts["user"] = "echoo_denied"
    with psycopg.connect(make_conninfo(**opts), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT echoo_pgmq.publish(%s,%s,'echoo_test_user')", (queue, b"forged"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"denied"))


def test_upgrade_failure_rolls_back_and_retry_preserves_data(dsn, admin):
    # A separate DB avoids changing the AMQP worker's database under a running test.
    database = "upgrade_" + uuid.uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    options = conninfo_to_dict(dsn)
    options["dbname"] = database
    try:
        with psycopg.connect(make_conninfo(**options), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.0'")
            conn.execute("SELECT echoo_pgmq.create_queue('upgrade_test')")
            conn.execute("SELECT echoo_pgmq.grant_queue('upgrade_test',session_user::text)")
            ident = conn.execute("SELECT echoo_pgmq.enqueue('upgrade_test',%s)", (b"\x00\xffupgrade",)).fetchone()[0]
            with pytest.raises(psycopg.errors.DivisionByZero):
                with conn.transaction():
                    conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.1'")
                    conn.execute("SELECT 1 / 0")  # deliberate transactional failure injection
            assert conn.execute("SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq'").fetchone()[0] == "0.1.0"
            assert conn.execute("SELECT to_regclass('echoo_pgmq.queue_stats')").fetchone()[0] is None
            assert conn.execute("SELECT body FROM echoo_pgmq.messages WHERE id=%s", (ident,)).fetchone()[0] == b"\x00\xffupgrade"
            conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.1'")
            assert conn.execute("SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq'").fetchone()[0] == "0.1.1"
            assert conn.execute("SELECT message_count FROM echoo_pgmq.queue_stats WHERE queue_name='upgrade_test'").fetchone()[0] == 1
            assert conn.execute("SELECT body FROM echoo_pgmq.messages WHERE id=%s", (ident,)).fetchone()[0] == b"\x00\xffupgrade"
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def _scratch_database(admin, dsn, prefix):
    database = prefix + uuid.uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    options = conninfo_to_dict(dsn)
    options["dbname"] = database
    return database, make_conninfo(**options)


def test_drop_queue_upgrade_reclaims_global_capacity(dsn, admin):
    database, scratch = _scratch_database(admin, dsn, "dropq_")
    options = conninfo_to_dict(scratch)
    options["user"] = "echoo_test_user"
    try:
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.0'")
            for name in ("drop/me", "keep/me"):
                conn.execute("SELECT echoo_pgmq.create_queue(%s)", (name,))
                conn.execute("SELECT echoo_pgmq.grant_queue(%s,session_user::text)", (name,))
            conn.execute("SELECT echoo_pgmq.grant_queue('drop/me','echoo_test_user')")
            conn.execute("SELECT echoo_pgmq.enqueue('drop/me',%s,'idem')", (b"ready-1",))
            conn.execute("SELECT echoo_pgmq.enqueue('drop/me',%s)", (b"ready-two",))
            leased = conn.execute("SELECT * FROM echoo_pgmq.read('drop/me',%s,3600)", (uuid.uuid4(),)).fetchone()
            rejected_owner = uuid.uuid4()
            conn.execute("SELECT echoo_pgmq.enqueue('drop/me',%s)", (b"dead",))
            rejected = conn.execute("SELECT * FROM echoo_pgmq.read('drop/me',%s,3600)", (rejected_owner,)).fetchone()
            assert conn.execute("SELECT echoo_pgmq.reject('drop/me',%s,%s,%s)", (rejected[0], rejected[1], rejected_owner)).fetchone()[0]
            conn.execute("SELECT echoo_pgmq.enqueue('keep/me',%s)", (b"kept",))
            assert leased is not None
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (4, 7 + 9 + 4 + 4)
            with pytest.raises(psycopg.errors.UndefinedFunction):
                with conn.transaction():
                    conn.execute("SELECT echoo_pgmq.drop_queue('drop/me')")
            conn.execute("ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'")
            assert conn.execute("SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq'").fetchone()[0] == "0.1.2"
            with psycopg.connect(make_conninfo(**options), autocommit=True) as user:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    user.execute("SELECT echoo_pgmq.drop_queue('drop/me')")
            assert conn.execute("SELECT echoo_pgmq.drop_queue('drop/me')").fetchone()[0] == 3
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (1, 4)
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name='keep/me'").fetchone() == (1, 4)
            assert conn.execute("SELECT count(*) FROM echoo_pgmq.queue_acl a LEFT JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.queue_id IS NULL").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM echoo_pgmq.idempotency").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM echoo_pgmq.messages").fetchone()[0] == 1
            with pytest.raises(psycopg.errors.UndefinedObject):
                conn.execute("SELECT echoo_pgmq.drop_queue('drop/me')")
            assert conn.execute("SELECT echoo_pgmq.drop_queue('drop/me',true)").fetchone()[0] is None
            # The name is immediately reusable and the old ACL did not survive.
            conn.execute("SELECT echoo_pgmq.create_queue('drop/me')")
            assert not conn.execute("SELECT echoo_pgmq.authorize('drop/me','echoo_test_user','produce')").fetchone()[0]
            assert conn.execute("SELECT echoo_pgmq.drop_queue('drop/me')").fetchone()[0] == 0
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_drop_queue_fails_fast_while_a_message_is_locked(dsn, admin):
    database, scratch = _scratch_database(admin, dsn, "droplock_")
    try:
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.2'")
            conn.execute("SELECT echoo_pgmq.create_queue('busy')")
            conn.execute("SELECT echoo_pgmq.grant_queue('busy',session_user::text)")
            conn.execute("SELECT echoo_pgmq.enqueue('busy',%s)", (b"payload",))
            with psycopg.connect(scratch) as consumer:
                owner = uuid.uuid4()
                receipt = consumer.execute("SELECT * FROM echoo_pgmq.read('busy',%s,60)", (owner,)).fetchone()
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    conn.execute("SELECT echoo_pgmq.drop_queue('busy')")
                # The failed drop released its locks; the open consumer can still ACK.
                assert consumer.execute("SELECT echoo_pgmq.ack('busy',%s,%s,%s)", (receipt[0], receipt[1], owner)).fetchone()[0]
                consumer.commit()
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (0, 0)
            conn.execute("SELECT echoo_pgmq.enqueue('busy',%s)", (b"again",))
            assert conn.execute("SELECT echoo_pgmq.drop_queue('busy')").fetchone()[0] == 1
            assert conn.execute("SELECT message_count,total_bytes FROM echoo_pgmq.limits").fetchone() == (0, 0)
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
