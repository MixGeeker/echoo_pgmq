"""Ordinary worker-plan reuse across transactions, quota errors and benign DDL."""
import uuid

from proton import Delivery, Message
from proton.utils import SendException
from psycopg import sql
import pytest

from conftest import eventually

pytestmark = pytest.mark.integration


def retained(admin, queue):
    return admin.execute(
        "SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name=%s",
        (queue,),
    ).fetchone()


def worker_pid(admin):
    return admin.execute(
        "SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'"
    ).fetchone()[0]


def roundtrip(admin, connection, queue, body, *, empty_claim=False):
    """Open fresh links so authorize is exercised alongside the other plans."""
    assert retained(admin, queue) == (0, 0)
    sender = connection.create_sender(queue)
    receiver = connection.create_receiver(queue, credit=0)
    if empty_claim:
        # Drain credit on an empty queue is acknowledged only after an empty
        # worker claim. This gives a protocol barrier rather than a timed sleep.
        receiver.link.drain(1)
        connection.wait(lambda: receiver.link.credit == 0, timeout=5)
        assert retained(admin, queue) == (0, 0)
    original = Message(body=body, id=uuid.uuid4())
    assert sender.send(original).remote_state == Delivery.ACCEPTED
    assert retained(admin, queue) == (1, len(original.encode()))
    received = receiver.receive(timeout=5)
    assert (received.body, received.id) == (original.body, original.id)
    receiver.accept()
    receiver.close()  # Flush the disposition before checking durable deletion.
    sender.close()
    eventually(lambda: retained(admin, queue) == (0, 0))


def test_repeated_worker_operations_and_empty_claims(admin, queue, connect_amqp):
    other = "plan_reuse_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s)", (other,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s,'echoo_test_user')", (other,))
    connection = connect_amqp()
    pid = worker_pid(admin)
    # Exceed PostgreSQL's initial five custom-plan executions, varying queue
    # and payload parameters while the same process retains only four plans.
    for index in range(8):
        target = (queue, other)[index % 2]
        roundtrip(admin, connection, target, f"cycle-{index}".encode(), empty_claim=True)
    assert retained(admin, queue) == retained(admin, other) == (0, 0)
    assert worker_pid(admin) == pid


def test_capacity_rejection_then_recovery_uses_same_worker(admin, connect_amqp):
    queue = "plan_capacity_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s,1,4096,1024,5)", (queue,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s,'echoo_test_user')", (queue,))
    connection = connect_amqp()
    pid = worker_pid(admin)
    for index in range(8):
        roundtrip(admin, connection, queue, f"warm-{index}".encode())

    sender = connection.create_sender(queue)
    held = Message(body=b"retained-at-capacity")
    assert sender.send(held).remote_state == Delivery.ACCEPTED
    before = retained(admin, queue)
    assert before == (1, len(held.encode()))
    with pytest.raises(SendException) as rejected:
        sender.send(Message(body=b"over-capacity"), timeout=5)
    assert rejected.value.state == Delivery.REJECTED
    assert retained(admin, queue) == before

    receiver = connection.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=5).body == held.body
    receiver.accept()
    receiver.close()
    eventually(lambda: retained(admin, queue) == (0, 0))

    # Reuse the very sender whose SQL transaction aborted on the quota limit.
    recovered = Message(body=b"after-capacity-release")
    assert sender.send(recovered).remote_state == Delivery.ACCEPTED
    assert retained(admin, queue) == (1, len(recovered.encode()))
    healthy = connection.create_receiver(queue, credit=1)
    assert healthy.receive(timeout=5).body == recovered.body
    healthy.accept()
    healthy.close()
    sender.close()
    eventually(lambda: retained(admin, queue) == (0, 0))
    assert worker_pid(admin) == pid


def test_function_cost_change_revalidates_worker_plans(admin, queue, connect_amqp):
    signatures = (
        "echoo_pgmq.authorize(text,text,text)",
        "echoo_pgmq.publish(text,bytea,text)",
        "echoo_pgmq.claim(text,text,uuid,integer)",
        "echoo_pgmq.settle(text,bigint,bigint,uuid,text,text)",
    )
    costs = [admin.execute("SELECT procost FROM pg_proc WHERE oid=%s::regprocedure",
                           (signature,)).fetchone()[0] for signature in signatures]
    connection = connect_amqp()
    pid = worker_pid(admin)
    for index in range(8):
        roundtrip(admin, connection, queue, f"before-ddl-{index}".encode())

    try:
        with admin.transaction():
            for signature, cost in zip(signatures, costs):
                # COST is planner metadata only: keep bodies, permissions,
                # SECURITY DEFINER and every function signature unchanged.
                admin.execute(sql.SQL("ALTER FUNCTION {} COST {}").format(
                    sql.SQL(signature), sql.Literal(cost + 1)))
        for index in range(8):
            roundtrip(admin, connection, queue, f"after-ddl-{index}".encode(), empty_claim=True)
        assert worker_pid(admin) == pid
    finally:
        with admin.transaction():
            for signature, cost in zip(signatures, costs):
                admin.execute(sql.SQL("ALTER FUNCTION {} COST {}").format(
                    sql.SQL(signature), sql.Literal(cost)))
    roundtrip(admin, connection, queue, b"after-cost-restoration", empty_claim=True)
    assert worker_pid(admin) == pid
