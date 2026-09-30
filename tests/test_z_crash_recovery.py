"""Owned isolated cluster only. SIGKILL/taskkill is NOT a physical power cut."""
import os
import signal
import subprocess
import time
import uuid

import psycopg
import pytest
from proton import Delivery, Message

pytestmark = [pytest.mark.integration, pytest.mark.crash]


def reconnect(dsn, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError as error:
            last = error
            time.sleep(0.1)
    raise AssertionError(f"PostgreSQL did not recover: {last}")


def test_worker_process_kill_recovers_accepted_message(dsn, admin, queue, connect_amqp):
    assert os.environ.get("ECHOO_TEST_PGDATA"), "crash test requires the owned cluster harness"
    connection = connect_amqp()
    assert connection.create_sender(queue).send(Message(body=b"accepted-before-kill")).remote_state == Delivery.ACCEPTED
    pid = admin.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True, capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)
    # PostgreSQL may restart sibling backends as part of shared-memory recovery.
    time.sleep(0.25)
    with reconnect(dsn) as check:
        row = check.execute("SELECT body FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()
        assert row is not None
        decoded = Message()
        decoded.decode(row[0])
        assert decoded.body == b"accepted-before-kill"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            replacement = check.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()
            if replacement and replacement[0] != pid:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("PostgreSQL did not restart the native worker")
    retried = connect_amqp()
    receiver = retried.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=8).body == b"accepted-before-kill"
    receiver.accept()
    retried.close()


def test_immediate_cluster_restart_preserves_committed_not_uncommitted(dsn, user_dsn, admin, queue):
    data, ctl = os.environ["ECHOO_TEST_PGDATA"], os.environ["ECHOO_TEST_PGCTL"]
    committed = psycopg.connect(user_dsn, autocommit=True)
    pending = psycopg.connect(user_dsn, autocommit=False)
    try:
        committed.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"committed"))
        pending.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"uncommitted"))
        subprocess.run([ctl, "-D", data, "-m", "immediate", "-w", "stop"], check=True)
        subprocess.run([ctl, "-D", data, "-l", os.environ["ECHOO_TEST_LOG"], "-w", "start"], check=True)
        with reconnect(dsn) as check:
            rows = check.execute("SELECT body FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchall()
            assert rows == [(b"committed",)]
    finally:
        committed.close()
        pending.close()
