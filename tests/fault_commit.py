"""Explicit test-only suite: pytest tests/fault_commit.py on a hook-enabled build.

The filename deliberately is not test_*.py: normal candidate artifacts compile
the injection hook out. This suite fails if invoked on a non-hook build.
"""
import time

from proton import Delivery, Link, Message, ProtonException
from proton.reactor import ReceiverOption
import pytest

from conftest import eventually


class DurableSettlement(ReceiverOption):
    def apply(self, link):
        link.snd_settle_mode = Link.SND_UNSETTLED
        link.rcv_settle_mode = Link.RCV_SECOND


def test_settlement_sql_success_is_not_reported_when_commit_boundary_fails(admin, queue, connect_amqp):
    assert admin.execute("SELECT current_setting('echoo_pgmq.test_fail_settle_before_commit')").fetchone()[0] == "off"
    connection = connect_amqp()
    assert connection.create_sender(queue).send(Message(body=b"commit-failure-proof", durable=True)).remote_state == Delivery.ACCEPTED
    receiver = connection.create_receiver(queue, credit=1, options=DurableSettlement())
    assert receiver.receive(timeout=5).body == b"commit-failure-proof"
    delivery = receiver.fetcher.unsettled[0]
    admin.execute("ALTER SYSTEM SET echoo_pgmq.test_fail_settle_before_commit='on'")
    admin.execute("SELECT pg_reload_conf()")
    eventually(lambda: admin.execute("SELECT current_setting('echoo_pgmq.test_fail_settle_before_commit')").fetchone()[0] == "on")
    # Both backends process the reload signal independently. Wait for the worker
    # to return to its event loop before submitting the disposition.
    time.sleep(.2)
    try:
        delivery.update(Delivery.ACCEPTED)
        # In SECOND mode the client does not locally settle before the server.
        with pytest.raises(ProtonException):
            connection.wait(lambda: delivery.remote_state != 0 or delivery.settled, timeout=5)
        assert delivery.remote_state != Delivery.ACCEPTED
        row = admin.execute("SELECT state,body FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()
        assert row and row[0] == "inflight", "failed commit must roll back SQL deletion"
        assert admin.execute("SELECT message_count FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone()[0] == 1
    finally:
        admin.execute("ALTER SYSTEM RESET echoo_pgmq.test_fail_settle_before_commit")
        admin.execute("SELECT pg_reload_conf()")
        eventually(lambda: admin.execute("SELECT current_setting('echoo_pgmq.test_fail_settle_before_commit')").fetchone()[0] == "off")
    retry = connect_amqp()
    consumer = retry.create_receiver(queue, credit=1)
    assert consumer.receive(timeout=7).body == b"commit-failure-proof"
    consumer.accept()
    consumer.close()
    eventually(lambda: admin.execute("SELECT message_count FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone()[0] == 0)
