"""Ordinary wire interoperability with independent JS rhea, not Proton bindings.

Infrastructure or protocol failures are failures, never skipped/xfail outcomes.
The harness creates disposable certificates and injects only loopback endpoints.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from conftest import eventually

pytestmark = pytest.mark.integration
INTEROP = Path(__file__).parent / "interop"


def rhea(operation, address, profile):
    node = shutil.which("node")
    assert node, "Node.js is required; install the locked tests/interop dependencies first"
    assert (INTEROP / "node_modules" / "rhea" / "package.json").is_file(), (
        "Run npm ci --prefix tests/interop --ignore-scripts --no-audit --no-fund")
    result = subprocess.run([node, str(INTEROP / "rhea_client.js"), operation, address, profile],
                            env=os.environ.copy(), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, f"rhea {operation} failed:\n{result.stdout}\n{result.stderr}"
    output = json.loads(result.stdout)
    assert (output["client"], output["version"]) == ("rhea", "3.0.5")
    assert output["address"] == address
    assert output["tls_sessions"]
    assert all(session["authorized"] and session["servername"] == "localhost"
               for session in output["tls_sessions"])
    evidence = os.environ.get("ECHOO_TEST_INTEROP_REPORT")
    if evidence:
        # Exclude the body/wire fixture from the compact, credential-free record.
        record = {key: value for key, value in output.items() if key != "wire_base64"}
        with Path(evidence).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return output


def publish_and_verify_storage(admin, address, profile):
    published = rhea("publish", address, profile)
    assert (published["published"], published["accepted"]) == (1, 1)
    rows = admin.execute("""SELECT body, attempts FROM echoo_pgmq.messages
        JOIN echoo_pgmq.queues USING(queue_id) WHERE name=%s""", (address,)).fetchall()
    assert rows == [(base64.b64decode(published["wire_base64"], validate=True), 0)], (
        "the single accepted rhea message must be stored with its exact encoded wire bytes")


def verify_consumed(admin, address, output, profile):
    assert output["metadata_verified"]
    assert output["accepted"] == 1
    assert output["remote_settlement_confirmed"] == (profile == "manual-second")
    eventually(lambda: admin.execute("SELECT message_count FROM echoo_pgmq.queues WHERE name=%s",
                                     (address,)).fetchone()[0] == 0)
    assert admin.execute("""SELECT count(*) FROM echoo_pgmq.messages
        JOIN echoo_pgmq.queues USING(queue_id) WHERE name=%s""", (address,)).fetchone()[0] == 0


@pytest.mark.parametrize("address_kind", ["plain", "path"])
@pytest.mark.parametrize("profile", ["default-first", "manual-second"])
def test_rhea_binary_metadata_roundtrip(admin, address_kind, profile):
    suffix = uuid.uuid4().hex
    address = "rhea_" + suffix if address_kind == "plain" else "erp/orders.store-17_" + suffix
    admin.execute("SELECT echoo_pgmq.create_queue(%s)", (address,))
    admin.execute("SELECT echoo_pgmq.grant_queue(%s, 'echoo_test_user')", (address,))
    publish_and_verify_storage(admin, address, profile)
    output = rhea("roundtrip", address, profile)
    assert output["received"] == 1
    assert output["tls_sessions"][0]["sasl"] == "direct"
    verify_consumed(admin, address, output, profile)


@pytest.mark.parametrize("profile", ["default-first", "manual-second"])
def test_rhea_release_redelivers(admin, queue, profile):
    publish_and_verify_storage(admin, queue, profile)
    output = rhea("release", queue, profile)
    assert (output["received"], output["released"]) == (2, 1)
    assert output["tls_sessions"][0]["sasl"] == "anonymous"
    verify_consumed(admin, queue, output, profile)


@pytest.mark.parametrize("profile", ["default-first", "manual-second"])
def test_rhea_graceful_reconnect_redelivers(admin, queue, profile):
    publish_and_verify_storage(admin, queue, profile)
    output = rhea("reconnect", queue, profile)
    assert output["reconnected"] and output["received"] == 2
    assert len(output["tls_sessions"]) == 2
    assert all(session["sasl"] == "external" for session in output["tls_sessions"])
    verify_consumed(admin, queue, output, profile)
