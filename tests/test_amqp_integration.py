"""Real AMQP 1.0 interoperability, settlement, authorization and protocol limits."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit
import uuid

from proton import Delivery, Message, ProtonException, symbol
from proton.utils import SendException
import pytest

from conftest import eventually

pytestmark = pytest.mark.integration


def count(admin, queue):
    return admin.execute("SELECT message_count FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone()[0]


def test_binary_and_metadata_roundtrip_preserves_encoded_wire(admin, queue, connect_amqp):
    connection = connect_amqp()
    original = Message(body=bytes(range(256)) + b"\x00\xff\x00", id=uuid.uuid4(),
                       correlation_id=uuid.uuid4(), subject="门店/二进制", content_type="application/octet-stream",
                       durable=True, priority=7, ttl=17.0, address="payload-address", reply_to="reply/queue",
                       creation_time=1700000000.0, expiry_time=1800000000.0,
                       properties={"store": "shop-17", "sequence": 42, "flag": True},
                       annotations={symbol("x-echoo-origin"): "erp"})
    wire = original.encode()
    delivery = connection.create_sender(queue).send(original)
    assert delivery.remote_state == Delivery.ACCEPTED
    stored = admin.execute("SELECT body FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0]
    assert stored == wire
    receiver = connection.create_receiver(queue, credit=1)
    received = receiver.receive(timeout=5)
    for attr in ("body", "id", "correlation_id", "subject", "content_type", "durable", "priority", "ttl", "address", "reply_to",
                 "creation_time", "expiry_time", "properties", "annotations"):
        assert getattr(received, attr) == getattr(original, attr), attr
    receiver.accept()
    receiver.close()
    eventually(lambda: count(admin, queue) == 0)


def test_sender_accepted_is_not_sent_before_commit(admin, queue, connect_amqp):
    # A real table lock delays INSERT/COMMIT in the native worker. No sleeps are
    # used as evidence: wait for PostgreSQL to report that blocked backend.
    def send():
        connection = connect_amqp()
        try:
            return connection.create_sender(queue).send(Message(body=b"commit-barrier"), timeout=10).remote_state
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with admin.transaction():
            admin.execute("LOCK TABLE echoo_pgmq.messages IN SHARE MODE")
            future = executor.submit(send)
            eventually(lambda: admin.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='echoo_pgmq_worker' AND wait_event_type='Lock'").fetchone()[0] > 0)
            assert not future.done(), "sender was settled before the locked INSERT could commit"
            assert count(admin, queue) == 0
        assert future.result(timeout=12) == Delivery.ACCEPTED
    assert count(admin, queue) == 1


def test_abandoned_delivery_is_redelivered(admin, queue, connect_amqp):
    first = connect_amqp()
    first.create_sender(queue).send(Message(body=b"retry-me"))
    receiver = first.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=5).body == b"retry-me"
    first.close()  # close without a disposition; durable lease remains
    second = connect_amqp()
    retry = second.create_receiver(queue, credit=1)
    assert retry.receive(timeout=7).body == b"retry-me"
    attempts = admin.execute("SELECT attempts FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0]
    assert attempts >= 2
    retry.accept()
    retry.close()
    eventually(lambda: count(admin, queue) == 0)


def test_late_ack_does_not_delete_new_delivery(admin, queue, connect_amqp):
    first = connect_amqp()
    first.create_sender(queue).send(Message(body=b"lease-generation"))
    old = first.create_receiver(queue, credit=1)
    assert old.receive(timeout=5).body == b"lease-generation"
    second = connect_amqp()
    new = second.create_receiver(queue, credit=1)
    assert new.receive(timeout=7).body == b"lease-generation"
    current = admin.execute("SELECT generation FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0]
    assert current >= 2
    try:
        old.accept()
        old.close()  # expiry can already have closed the old link
    except ProtonException:
        pass
    assert count(admin, queue) == 1
    new.accept()
    new.close()
    eventually(lambda: count(admin, queue) == 0)


def test_release_retries_and_reject_retains_dead_letter(admin, queue, connect_amqp):
    connection = connect_amqp()
    connection.create_sender(queue).send(Message(body=b"poison"))
    receiver = connection.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=5).body == b"poison"
    receiver.release(delivered=False)
    assert receiver.receive(timeout=5).body == b"poison"
    receiver.reject()
    receiver.close()
    eventually(lambda: admin.execute("SELECT state FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0] == "dead", timeout=5)
    assert count(admin, queue) == 1  # dead letters count toward retained capacity


@pytest.mark.parametrize("identity", [None, "unknown"])
def test_missing_or_unauthorized_certificate_cannot_publish(admin, queue, connect_amqp, identity):
    with pytest.raises((ProtonException, OSError)) as captured:
        connection = connect_amqp(identity, timeout=3)
        connection.create_sender(queue).send(Message(body=b"forbidden"), timeout=3)
    # Broad transport exception family is intentional: TLS fail vs link refusal
    # differs by Proton backend. The protected data assertion is authoritative.
    assert not isinstance(captured.value, (AssertionError, KeyError, FileNotFoundError))
    assert count(admin, queue) == 0


def test_unknown_address_is_refused(admin, connect_amqp):
    name = "nonexistent_" + uuid.uuid4().hex
    connection = connect_amqp()
    with pytest.raises((ProtonException, OSError)):
        connection.create_sender(name).send(Message(body=b"do-not-create"), timeout=3)
    assert admin.execute("SELECT count(*) FROM echoo_pgmq.queues WHERE name=%s", (name,)).fetchone()[0] == 0


def test_message_size_limit_is_enforced_and_worker_survives(admin, queue, connect_amqp):
    connection = connect_amqp()
    with pytest.raises((ProtonException, OSError)):
        connection.create_sender(queue).send(Message(body=b"x" * 70000), timeout=5)
    assert count(admin, queue) == 0
    healthy = connect_amqp()
    assert healthy.create_sender(queue).send(Message(body=b"after-limit")).remote_state == Delivery.ACCEPTED
    assert count(admin, queue) == 1


def test_link_limit_is_enforced(admin, queue, connect_amqp):
    connection = connect_amqp()
    links = [connection.create_sender(queue, name=f"bounded-{i}") for i in range(4)]
    assert len(links) == 4
    with pytest.raises((ProtonException, OSError)):
        connection.create_sender(queue, name="over-limit")
    assert count(admin, queue) == 0


def test_connection_limit_is_enforced(admin, connect_amqp):
    connections = [connect_amqp() for _ in range(8)]
    assert len(connections) == 8
    with pytest.raises((ProtonException, OSError)):
        connect_amqp(timeout=2)
    assert admin.execute("SELECT 1").fetchone()[0] == 1


def tls_socket():
    url = urlsplit(os.environ["ECHOO_TEST_AMQP_URL"])
    certs = Path(os.environ["ECHOO_TEST_CERT_DIR"])
    ctx = ssl.create_default_context(cafile=str(certs / "ca.pem"))
    ctx.load_cert_chain(str(certs / "client.pem"), str(certs / "client.key"))
    sock = ctx.wrap_socket(socket.create_connection((url.hostname, url.port), timeout=3), server_hostname="localhost")
    sock.settimeout(3)
    return sock


@pytest.mark.parametrize("frame", [
    b"NOTAMQP!",                                  # invalid protocol header
    b"AMQP\x00\x01\x00\x00" + struct.pack("!IBBH", 8, 0, 0, 0),  # invalid data offset
    b"AMQP\x00\x01\x00\x00" + struct.pack("!IBBH", 2**31, 2, 0, 0),  # enormous announced frame
])
def test_malformed_frames_close_connection_without_worker_crash(admin, queue, connect_amqp, frame):
    before = admin.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]
    closed = False
    with tls_socket() as sock:
        try:
            sock.sendall(frame)
            while sock.recv(4096):
                pass
            closed = True
        except (ConnectionResetError, ssl.SSLError, BrokenPipeError):
            closed = True
        except socket.timeout:
            pass
    assert closed, "malformed/oversized protocol input was not closed within 3 seconds"
    after = admin.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]
    assert after == before, "malformed input crashed/restarted the native worker"
    connection = connect_amqp()
    assert connection.create_sender(queue).send(Message(body=b"still-healthy")).remote_state == Delivery.ACCEPTED


@pytest.mark.parametrize("wire", [b"", b"not-an-amqp-message", b"\x00\x53\x75\xb0\xff\xff\xff\xff"])
def test_malformed_encoded_message_is_rejected(admin, queue, connect_amqp, wire):
    connection = connect_amqp()
    sender = connection.create_sender(queue)
    connection.wait(lambda: sender.link.credit > 0, timeout=5)
    delivery = sender.link.delivery("malformed-" + uuid.uuid4().hex)
    if wire:
        sender.link.stream(wire)
    sender.link.advance()
    connection.wait(lambda: delivery.remote_state != 0, timeout=5)
    assert delivery.remote_state == Delivery.REJECTED
    delivery.settle()
    sender.close()
    assert count(admin, queue) == 0


def test_rejected_connection_stress_keeps_worker_alive(admin, queue, connect_amqp):
    """Regression for rejected-attach endpoint double-free; 120 real attempts."""
    pid = admin.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]
    for attempt in range(120):
        connection = None
        try:
            with pytest.raises((ProtonException, OSError)):
                identity = [None, "unknown", "client"][attempt % 3]
                connection = connect_amqp(identity, timeout=3)
                address = "missing_stress_" + uuid.uuid4().hex if identity == "client" else queue
                connection.create_sender(address).send(Message(body=b"must-not-store"), timeout=3)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (ProtonException, OSError):
                    pass
        assert admin.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0] == pid
        if attempt % 10 == 9:
            healthy = connect_amqp()
            healthy.create_sender(queue).close()
            healthy.close()
    assert count(admin, queue) == 0


def test_raw_sql_poison_is_quarantined_and_valid_messages_continue(admin, client, queue, connect_amqp):
    poison = b"not-an-amqp-message"
    poison_id = client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, poison)).fetchone()[0]
    connection = connect_amqp()
    receiver = connection.create_receiver(queue, credit=1)
    from proton import Timeout
    with pytest.raises(Timeout):
        receiver.receive(timeout=2)
    receiver.close()
    assert admin.execute("SELECT state,attempts,body FROM echoo_pgmq.messages WHERE id=%s", (poison_id,)).fetchone() == ("dead", 1, poison)
    assert connection.create_sender(queue).send(Message(body=b"valid-after-poison")).remote_state == Delivery.ACCEPTED
    good = connection.create_receiver(queue, name="healthy-after-poison", credit=1)
    assert good.receive(timeout=5).body == b"valid-after-poison"
    good.accept()
    good.close()
    eventually(lambda: count(admin, queue) == 1)  # retained poison only


def test_long_sql_transaction_backpressures_other_queue_then_recovers(admin, user_dsn, queue, connect_amqp):
    import psycopg
    other = "contended_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s)", (other,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s,'echoo_test_user')", (other,))
    connection = connect_amqp()
    sender = connection.create_sender(other)
    with psycopg.connect(user_dsn, autocommit=False) as blocker:
        blocker.execute("SELECT echoo_pgmq.enqueue_binary(%s,%s)", (queue, b"outer-transaction"))
        with pytest.raises(SendException) as rejected:
            sender.send(Message(body=b"blocked-on-global-quota"), timeout=5)
        assert rejected.value.state == Delivery.REJECTED
        assert count(admin, other) == 0
        blocker.rollback()
    assert sender.send(Message(body=b"after-lock-release"), timeout=5).remote_state == Delivery.ACCEPTED
    assert count(admin, other) == 1


def test_sql_empty_and_over_worker_limit_do_not_starve_valid_message(admin, client, queue, connect_amqp):
    empty = client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"")).fetchone()[0]
    large = client.execute("SELECT echoo_pgmq.enqueue(%s,%s)", (queue, b"x" * 65537)).fetchone()[0]
    valid = client.execute("SELECT echoo_pgmq.enqueue_binary(%s,%s)", (queue, b"valid-behind-poison")).fetchone()[0]
    connection = connect_amqp()
    receiver = connection.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=8).body == b"valid-behind-poison"
    receiver.accept()
    receiver.close()
    eventually(lambda: count(admin, queue) == 2)
    assert admin.execute("SELECT id,state,octet_length(body) FROM echoo_pgmq.messages WHERE id=ANY(%s) ORDER BY id", ([empty, large],)).fetchall() == [(empty, "dead", 0), (large, "dead", 65537)]
    assert admin.execute("SELECT count(*) FROM echoo_pgmq.messages WHERE id=%s", (valid,)).fetchone()[0] == 0
    assert admin.execute("SELECT message_count,total_bytes FROM echoo_pgmq.queues WHERE name=%s", (queue,)).fetchone() == (2, 65537)


def test_presettled_publisher_is_refused(admin, queue, connect_amqp):
    from proton.reactor import AtMostOnce
    connection = connect_amqp()
    with pytest.raises(ProtonException):
        connection.create_sender(queue, options=AtMostOnce())
    assert count(admin, queue) == 0
