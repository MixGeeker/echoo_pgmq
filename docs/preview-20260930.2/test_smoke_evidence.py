"""Offline normal harness correctness; no PG/network/messages are run."""
import hashlib
import json
from pathlib import Path
import copy
import unittest
from unittest.mock import MagicMock, patch
from run_windows_smoke import PREVIEW_STAGES, verify_policy, verify_sql_bridge_record, available_loopback_ports

class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.record = dict(operation='sql-binary', client='rhea', version='3.0.5', received=1,
                           accepted=1, remote_settlement_confirmed=True, metadata_verified=True,
                           sql_binary_envelope_verified=True, binary_body_bytes=12)
        self.policy = dict(preview_label='preview-20260930.2', max_encoded_message_bytes=65536,
                           worker_max_message_bytes=65536, global_max_message_bytes=65536,
                           global_message_count=0, global_total_bytes=0, retained_message_rows=0,
                           queues=[dict(name=f'preview/q{i}', queue_max_message_bytes=65536,
                                        effective_sql_max_message_bytes=65536, queue_message_count=0,
                                        queue_total_bytes=0, retained_message_rows=0) for i in range(5)])
    def test_runner_asks_os_for_two_distinct_loopback_ports(self):
        first, second = MagicMock(), MagicMock()
        first.__enter__.return_value = first
        second.__enter__.return_value = second
        first.getsockname.return_value = ('127.0.0.1', 61001)
        second.getsockname.return_value = ('127.0.0.1', 61002)
        with patch('run_windows_smoke.socket.socket', side_effect=[first, second]):
            self.assertEqual(available_loopback_ports(), (61001, 61002))
        first.bind.assert_called_once_with(('127.0.0.1', 0))
        second.bind.assert_called_once_with(('127.0.0.1', 0))

    def test_exact_guide_bytes_match_delivery_pins(self):
        here = Path(__file__).resolve().parent
        pins = json.loads((here / 'fixed-inputs.json').read_text(encoding='utf-8'))['guide_sha256']
        self.assertEqual(len(pins), 12)
        for relative, expected in pins.items():
            data = (here / 'guide' / relative).read_bytes()
            self.assertNotIn(b'\r\n', data, relative)
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected, relative)

    def test_complete_bridge_and_policy_evidence(self):
        verify_sql_bridge_record(self.record)
        verify_policy(self.policy)
        self.assertEqual(PREVIEW_STAGES, ('install','init','start','sql-demo','amqp-demo','policy-check','stop'))
    def test_local_ack_is_not_remote_commit_evidence(self):
        self.record['remote_settlement_confirmed'] = False
        with self.assertRaises(RuntimeError): verify_sql_bridge_record(self.record)
    def test_wrong_admission_configuration_is_not_pass(self):
        self.policy['global_max_message_bytes'] = 1048576
        with self.assertRaises(RuntimeError): verify_policy(self.policy)
    def test_missing_queue_evidence_is_not_pass(self):
        self.policy['queues'].pop()
        with self.assertRaises(RuntimeError): verify_policy(self.policy)
    def test_nonempty_demo_is_not_pass(self):
        self.policy['queues'][0]['retained_message_rows'] = 1
        with self.assertRaises(RuntimeError): verify_policy(self.policy)
