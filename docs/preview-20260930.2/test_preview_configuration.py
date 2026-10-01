"""Offline wrapper configuration regression: no sockets, server, keys or payload tests."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

GUIDE = Path(__file__).resolve().parent / 'guide'
EVIDENCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('preview_configuration_under_test', GUIDE / 'preview.py')
preview_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview_module)


class PreviewConfiguration(unittest.TestCase):
    def generate_configuration(self, policy):
        """Execute real init()/queue() with only external side effects replaced."""
        with tempfile.TemporaryDirectory(prefix='echoo-preview-offline-') as temporary:
            preview = preview_module.Preview(Path(temporary))
            queries, actions = [], []

            def load():
                preview.state = {'magic': preview_module.MAGIC, 'root': str(preview.root)}

            def initdb(argv, **kwargs):
                self.assertEqual(Path(argv[0]).stem, 'initdb')
                preview.data.mkdir()
                (preview.data / 'postgresql.conf').write_text('# offline initdb fixture\n')

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(preview_module, 'MAX_ENCODED_MESSAGE_BYTES', policy))
                stack.enter_context(patch.object(preview, 'load', side_effect=load))
                stack.enter_context(patch.object(preview, 'pg', side_effect=actions.append))
                stack.enter_context(patch.object(preview, 'sql', side_effect=lambda text, **kwargs: queries.append(text)))
                stack.enter_context(patch.object(preview_module, 'run', side_effect=initdb))
                stack.enter_context(patch.object(preview_module, 'generate', side_effect=lambda path: path.mkdir()))
                stack.enter_context(patch.object(preview_module, 'private_write'))
                stack.enter_context(patch.object(preview_module.secrets, 'token_hex', return_value='OFFLINE_TEST_PLACEHOLDER'))
                stack.enter_context(patch.object(preview_module.socket, 'socket'))
                stack.enter_context(patch.object(preview_module.uuid, 'uuid4', return_value=preview_module.uuid.UUID(int=0)))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                preview.init(55418, 56718)
                address = preview.queue('configuration')
            self.assertEqual(actions, ['start', 'stop'])
            self.assertEqual(len(queries), 3)
            self.assertEqual(queries[0], 'CREATE DATABASE echoo_preview;')
            config = (preview.data / 'postgresql.conf').read_text(encoding='utf-8')
            self.assertFalse((preview.root / '.pgpass-preview').exists())
            self.assertEqual(list(preview.certs.iterdir()), [])
            return queries[1], queries[2], config, address

    def assert_generated_policy(self, policy, generated):
        init_sql, queue_sql, config, address = generated
        # Parse the actual statements emitted through init, not a duplicated
        # formatter/helper or an assertion only against the Python constant.
        initialized = re.findall(r'INSERT INTO echoo_pgmq\.limits\s*\(singleton, max_message_bytes\)\s*VALUES\s*\(true,\s*(\d+)\)', init_sql)
        self.assertEqual(initialized, [str(policy)])
        self.assertLess(init_sql.index('CREATE EXTENSION'), init_sql.index('INSERT INTO echoo_pgmq.limits'))
        self.assertEqual(re.findall(r"^echoo_pgmq\.max_message_bytes='(\d+)'$", config, re.M), [str(policy)])
        self.assertEqual(queue_sql, f"SELECT echoo_pgmq.create_queue('{address}', p_max_message_bytes => {policy}); SELECT echoo_pgmq.grant_queue('{address}', 'echoo_test_user');")

    def test_shipped_configuration_emits_consistent_policy(self):
        generated = self.generate_configuration(65536)
        self.assert_generated_policy(65536, generated)
        for name, content in zip(('generated-init.sql', 'generated-queue.sql', 'generated-postgresql.conf'), generated[:3]):
            (EVIDENCE / name).write_text(content + ('\n' if not content.endswith('\n') else ''))

    def test_all_emission_paths_follow_the_single_policy(self):
        # Offline alternate policy proves all three real emission paths depend
        # on the single constant; this sends no message and measures no limit.
        self.assert_generated_policy(32768, self.generate_configuration(32768))

    def test_policy_query_and_smaller_queue_compatibility(self):
        preview = preview_module.Preview(Path(tempfile.gettempdir()) / 'echoo-preview-readonly-fixture')
        result = {'worker_max_message_bytes': 65536, 'global_max_message_bytes': 65536,
                  'queues': [{'name': 'preview/smaller', 'queue_max_message_bytes': 4096,
                              'effective_sql_max_message_bytes': 4096},
                             {'name': 'preview/default', 'queue_max_message_bytes': 1048576,
                              'effective_sql_max_message_bytes': 65536}]}
        with patch.object(preview, 'sql', return_value=json.dumps(result)) as sql:
            actual = preview.message_policy()
        query = sql.call_args.args[0]
        self.assertTrue(sql.call_args.kwargs['capture'])
        self.assertIn("current_setting('echoo_pgmq.max_message_bytes')::integer", query)
        self.assertIn('SELECT max_message_bytes FROM echoo_pgmq.limits WHERE singleton', query)
        self.assertIn('LEAST(l.max_message_bytes, q.max_message_bytes)', query)
        self.assertEqual(actual['queues'], result['queues'])
        self.assertEqual(actual['preview_label'], 'preview-20260930.2')

    def test_start_verifies_the_running_configuration(self):
        preview = preview_module.Preview(Path(tempfile.gettempdir()) / 'echoo-preview-start-fixture')
        calls = []
        with patch.object(preview, 'pg', side_effect=lambda action: calls.append(action)), \
             patch.object(preview, 'message_policy', side_effect=lambda: calls.append('policy')), \
             contextlib.redirect_stdout(io.StringIO()):
            preview.start()
        self.assertEqual(calls, ['start', 'policy'])

    def test_sql_binary_fixture_and_documented_byte_count_agree(self):
        payload = bytes.fromhex(preview_module.SQL_BINARY_PAYLOAD_HEX)
        client = (GUIDE / 'interop/rhea_client.js').read_text(encoding='utf-8')
        client_fixtures = re.findall(r"Buffer\.from\('([0-9a-f]+)', 'hex'\)", client)
        self.assertEqual(client_fixtures, [preview_module.SQL_BINARY_PAYLOAD_HEX])
        self.assertEqual(len(payload), 12)
        self.assertIn(f'入队 {len(payload)} 字节合成二进制', (GUIDE / 'README.zh-CN.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
