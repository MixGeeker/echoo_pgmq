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
                       durable=True, properties={"store": "shop-17", "sequence": 42, "flag": True},
                       annotations={symbol("x-echoo-origin"): "erp"})
    wire = original.encode()
    delivery = connection.create_sender(queue).send(original)
    assert delivery.remote_state == Delivery.ACCEPTED
    stored = admin.execute("SELECT body FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0]
    assert stored == wire
    receiver = connection.create_receiver(queue, credit=1)
    received = receiver.receive(timeout=5)
    for attr in ("body", "id", "correlation_id", "subject", "content_type", "durable", "properties", "annotations"):
        assert getattr(received, attr) == getattr(original, attr), attr
    receiver.accept()
    connection.wait(lambda: count(admin, queue) == 0, timeout=5)


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
    second.wait(lambda: count(admin, queue) == 0, timeout=5)


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
    old.accept()
    # Pump the old connection so the stale disposition reaches the worker.
    old.close()
    assert count(admin, queue) == 1
    new.accept()
    second.wait(lambda: count(admin, queue) == 0, timeout=5)


def test_release_retries_and_reject_retains_dead_letter(admin, queue, connect_amqp):
    connection = connect_amqp()
    connection.create_sender(queue).send(Message(body=b"poison"))
    receiver = connection.create_receiver(queue, credit=1)
    assert receiver.receive(timeout=5).body == b"poison"
    receiver.release(delivered=False)
    assert receiver.receive(timeout=5).body == b"poison"
    receiver.reject()
    connection.wait(lambda: admin.execute("SELECT state FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id) WHERE q.name=%s", (queue,)).fetchone()[0] == "dead", timeout=5)
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
    links = [connection.create_sender(queue) for _ in range(4)]
    assert len(links) == 4
    with pytest.raises((ProtonException, OSError)):
        connection.create_sender(queue)
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
