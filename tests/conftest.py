"""Black-box integration fixtures; missing infrastructure is a failure, not a skip."""
import os
from pathlib import Path
import time
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest


def eventually(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.025)
    raise AssertionError(f"condition not reached within {timeout}s; last={last!r}")


@pytest.fixture(scope="session")
def dsn():
    value = os.environ.get("ECHOO_TEST_DSN")
    if not value:
        raise pytest.UsageError("Use python scripts/run_integration.py; ECHOO_TEST_DSN is required")
    return value


@pytest.fixture
def admin(dsn):
    with psycopg.connect(dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture(scope="session")
def user_dsn(dsn):
    options = conninfo_to_dict(dsn)
    options["user"] = "echoo_test_user"
    return make_conninfo(**options)


@pytest.fixture
def client(user_dsn):
    with psycopg.connect(user_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def queue(admin):
    name = "t_" + uuid.uuid4().hex
    admin.execute("SELECT echoo_pgmq.create_queue(%s)", (name,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s, 'echoo_test_user')", (name,))
    return name


@pytest.fixture
def connect_amqp():
    from proton import SSLDomain
    from proton.utils import BlockingConnection
    certs = Path(os.environ["ECHOO_TEST_CERT_DIR"])
    connections = []

    def connect(identity="client", timeout=5):
        domain = SSLDomain(SSLDomain.MODE_CLIENT)
        if identity:
            if os.name == "nt":  # The Python wheel bundles Proton's SChannel backend.
                domain.set_credentials(str(certs / f"{identity}.p12"), identity, "")
            else:
                domain.set_credentials(str(certs / f"{identity}.pem"), str(certs / f"{identity}.key"), None)
        domain.set_trusted_ca_db(str(certs / ("ca.p12" if os.name == "nt" else "ca.pem")))
        domain.set_peer_authentication(SSLDomain.VERIFY_PEER_NAME)
        connection = BlockingConnection(os.environ["ECHOO_TEST_AMQP_URL"], ssl_domain=domain,
                                        sasl_enabled=False, timeout=timeout, reconnect=False)
        connections.append(connection)
        return connection
    yield connect
    for connection in reversed(connections):
        try:
            connection.close()
        except Exception:
            pass
