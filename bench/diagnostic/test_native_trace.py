#!/usr/bin/env python3
"""Focused diagnostic transformation/evidence-contract tests, no server injection."""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("instrument_native", HERE / "instrument_native.py")
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)


class InstrumentationContract(unittest.TestCase):
    def test_exact_reversible_same_transforms_both_fixed_sources(self):
        root = HERE.parents[1]
        for revision in (NATIVE.BASELINE, NATIVE.WAKE):
            original = NATIVE.git(root, "show", f"{revision}:{NATIVE.NATIVE}").decode()
            generated = NATIVE.instrument(original)
            self.assertEqual(NATIVE.uninstrument(generated), original)
            self.assertEqual(generated.count("echoo_diag_flush_normal_shutdown();"), 1)
            self.assertNotIn("size = send(connection->socket", generated)

    def test_anchor_requires_exactly_one_match(self):
        with self.assertRaises(ValueError):
            NATIVE.replace_once("abcabc", "abc", "xyz")
        with self.assertRaises(ValueError):
            NATIVE.replace_once("def", "abc", "xyz")

    def test_materialized_variants_differ_only_in_native_wake_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "sources"
            manifest = NATIVE.materialize(HERE.parents[1], out)
            a = manifest["variants"]["A"]["files"]
            b = manifest["variants"]["B"]["files"]
            self.assertEqual([path for path in a if a[path] != b[path]], [NATIVE.NATIVE])
            self.assertEqual((out / "A/src/echoo_diagnostic_native_trace.h").read_bytes(),
                             (out / "B/src/echoo_diagnostic_native_trace.h").read_bytes())
            self.assertEqual(manifest["test_hooks"], "OFF")

    def test_refuses_to_write_inside_checkout(self):
        with self.assertRaises(ValueError):
            NATIVE.materialize(HERE.parents[1], HERE / "generated-test-not-created")

    def test_native_observation_has_no_payload_decode_or_setting_changes(self):
        header = (HERE / "native_trace.h").read_text()
        self.assertNotIn("setsockopt(", header)
        self.assertNotIn("pn_message_decode(", header)
        self.assertIn("clock_gettime(CLOCK_MONOTONIC", header)
        self.assertIn("O_EXCL | O_NOFOLLOW", header)
        self.assertIn("#define ECHOO_DIAG_CAPACITY 1048576U", header)
        # File operations are all in the one normal-shutdown function.
        active, flush = header.split("echoo_diag_flush_normal_shutdown(void)")
        for text in ("fprintf(", "fputs(", "open(echoo_diag_path", "fsync("):
            self.assertNotIn(text, active)
            self.assertIn(text, flush)


class TraceContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "trace.jsonl"
        self.rows = []
        def event(name, connection=0, link=0, delivery=0, operation=0, **values):
            row = dict(record="event", event=name, seq=0, t_ns=0, loop=1,
                       connection=connection, link=link, delivery=delivery,
                       operation=operation, db_id=0, generation=0, value1=0,
                       value2=0, result=0, errno=0, tcp_nodelay=-1,
                       observation_errno=0, data_hex="")
            row.update(values)
            self.rows.append(row)
        event("worker_start")
        event("connection_open", 1, value1=5671, value2=31001, result=1, tcp_nodelay=0)
        event("link_open", 1, 1, value1=1, result=1, data_hex=b"diag-consumer".hex())
        event("connection_open", 2, value1=5671, value2=31002, result=1, tcp_nodelay=0)
        event("link_open", 2, 2, value1=0, result=1, data_hex=b"diag-producer".hex())
        event("delivery_map", 2, 2, 1, 1, result=1, data_hex=b"1".hex())
        event("publish_start", 2, 2, 1, 1)
        event("publish_return", 2, 2, 1, 1, result=1)
        event("accepted_queued", 2, 2, 1, 1, result=1)
        event("claim_start", 1, 1, 0, 2)
        event("claim_return", 1, 1, 0, 2, result=1, db_id=42, generation=1)
        event("delivery_map", 1, 1, 2, 2, result=1, db_id=42, generation=1, data_hex=b"42:1".hex())
        event("transfer_queued", 1, 1, 2, 2, result=1, db_id=42, generation=1)
        event("socket_send_start", 2, value1=1, value2=64, tcp_nodelay=0)
        event("socket_send_return", 2, value1=1, value2=64, result=64, tcp_nodelay=0)
        event("socket_send_start", 1, value1=1, value2=100, tcp_nodelay=0)
        event("socket_send_return", 1, value1=1, value2=100, result=100, tcp_nodelay=0)
        event("settle_start", 1, 1, 2, 3, db_id=42, generation=1)
        event("settle_return", 1, 1, 2, 3, result=1, db_id=42, generation=1)
        event("connection_visit", 2, loop=2, value1=1)
        event("connection_visit", 1, loop=2, value1=2)
        event("normal_shutdown", loop=2)
        self.footer = dict(record="footer", normal_shutdown=True, complete=True,
                           events=0, attempted_events=0, dropped_events=0,
                           clock_failures=0, identity_failures=0, observation_failures=0)
        for kind, name, port, tag in (("producer", "diag-producer", 31002, "1"),
                                      ("consumer", "diag-consumer", 31001, "42:1")):
            (self.root / f"{kind}-0.ready.json").write_text(json.dumps(dict(
                link_name=name, local=["127.0.0.1", port], peer=["127.0.0.1", 5671])))
            with gzip.open(self.root / f"{kind}-0.jsonl.gz", "wt") as stream:
                stream.write(json.dumps(dict(id="run/0/0", delivery_tag_hex=tag.encode().hex(),
                                             accepted=True, valid=True)) + "\n")
        self.write()

    def write(self):
        for index, row in enumerate(self.rows, 1):
            row.update(seq=index, t_ns=index * 1000)
        self.footer.update(events=len(self.rows), attempted_events=len(self.rows))
        metadata = dict(record="metadata", schema=1, pid=1234, clock="CLOCK_MONOTONIC", unit="ns",
                        clock_resolution_ns=1, identity_limit_bytes=128)
        self.path.write_text("\n".join(json.dumps(row) for row in [metadata, *self.rows, self.footer]) + "\n")

    def test_complete_exact_join_and_native_elapsed_only(self):
        result = NATIVE.validate_trace(self.path, self.root)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["message_stages"][0]["id"], "run/0/0")
        self.assertEqual(result["message_stages"][0]["db_id"], 42)
        self.assertEqual(result["message_stages"][0]["server_elapsed_ns"]["publish_db_ns"], 1000)
        self.assertTrue(result["consumer_before_producer_accept"])
        self.assertEqual(result["worker_pid"], 1234)

    def test_docker_forwarding_joins_unique_link_tag_not_ports(self):
        for path in self.root.glob("*.ready.json"):
            value = json.loads(path.read_text())
            value.update(network_layout="docker_loopback_port_forwarding", local=["127.0.0.1", 40001],
                         peer=["127.0.0.1", 49152])
            path.write_text(json.dumps(value))
        result = NATIVE.validate_trace(self.path, self.root)
        self.assertTrue(result["complete"], result)

    def test_direct_endpoint_disagreement_is_insufficient(self):
        path = self.root / "producer-0.ready.json"
        value = json.loads(path.read_text())
        value["local"][1] = 40001
        path.write_text(json.dumps(value))
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])

    def test_missing_trace_is_insufficient(self):
        result = NATIVE.validate_trace(self.root / "missing", self.root)
        self.assertFalse(result["complete"])

    def test_counter_loss_is_insufficient(self):
        for name in ("dropped_events", "clock_failures", "identity_failures", "observation_failures"):
            with self.subTest(name=name):
                self.footer[name] = 1
                self.write()
                self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])
                self.footer[name] = 0

    def test_missing_client_join_is_insufficient(self):
        self.assertFalse(NATIVE.validate_trace(self.path)["complete"])
        (self.root / "producer-0.ready.json").unlink()
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])

    def test_missing_settle_span_is_insufficient(self):
        self.rows = [row for row in self.rows if row["event"] != "settle_return"]
        self.write()
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])

    def test_wrong_actual_traversal_is_insufficient(self):
        visits = [row for row in self.rows if row["event"] == "connection_visit"]
        visits[0]["connection"], visits[1]["connection"] = 1, 2
        self.write()
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])

    def test_unobserved_tcp_nodelay_is_insufficient(self):
        next(row for row in self.rows if row["event"] == "socket_send_start")["tcp_nodelay"] = -1
        self.write()
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])

    def test_truncated_footer_is_insufficient(self):
        self.path.write_text("\n".join(self.path.read_text().splitlines()[:-1]))
        self.assertFalse(NATIVE.validate_trace(self.path, self.root)["complete"])


if __name__ == "__main__":
    unittest.main()
