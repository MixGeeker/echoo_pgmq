"""Ordinary delivery semantics around queue-specific empty-claim invalidation."""
import uuid

import pytest
from proton import Delivery, Message

from conftest import eventually

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('publisher_first', [False, True])
def test_empty_consumer_separate_connection_orders(admin, queue, connect_amqp, publisher_first):
    first, second = connect_amqp(), connect_amqp()
    publisher, consumer = (first, second) if publisher_first else (second, first)
    sender = publisher.create_sender(queue)
    receiver = consumer.create_receiver(queue, credit=0)
    for sequence in range(4):
        # Drain is a protocol barrier for a completed empty claim; no sleep is
        # used to guess whether the consumer has entered its polling delay.
        receiver.link.drain(1)
        consumer.wait(lambda: receiver.link.credit == 0, timeout=5)
        body = f'committed-{sequence}'.encode()
        message = Message(body=body, id=uuid.uuid4())
        assert sender.send(message).remote_state == Delivery.ACCEPTED
        # Invalidation is not a claim: without credit the durable row remains.
        assert admin.execute('SELECT message_count FROM echoo_pgmq.queues WHERE name=%s', (queue,)).fetchone() == (1,)
        received = receiver.receive(timeout=5)
        assert (received.id, received.body) == (message.id, body)
        receiver.accept()
        # Flush settlement and regain an explicit empty-claim barrier.
        receiver.link.drain(1)
        consumer.wait(lambda: receiver.link.credit == 0, timeout=5)
        eventually(lambda: admin.execute('SELECT message_count FROM echoo_pgmq.queues WHERE name=%s', (queue,)).fetchone() == (0,))
    receiver.close()
    sender.close()
