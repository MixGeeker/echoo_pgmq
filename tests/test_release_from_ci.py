"""Offline release safety tests. No GitHub access or credentials required."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('release_from_ci', Path(__file__).resolve().parents[1] / 'scripts/release_from_ci.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


class ReleaseTests(unittest.TestCase):
    def test_fixed_scope_and_matrix(self):
        config = json.loads((r.ROOT / 'releases/v0.1.0.json').read_text())
        self.assertEqual(config['tag'], 'v0.1.0')
        self.assertEqual(config['source_sha'], 'ef12d35447c7783f75ca123467208eb100533311')
        self.assertEqual(len({a['id'] for a in config['artifacts']}), 12)
        self.assertEqual(len({a['name'] for a in config['artifacts']}), 12)
        for a in config['artifacts']:
            self.assertRegex(a['digest'], r'^sha256:[0-9a-f]{64}$')

    def test_unsafe_zip_paths_rejected(self):
        for name in ('../x', '/x', 'a/../../x', 'a\\b', 'C:/x'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                r.safe_zip(zipped([(name, b'x')]))

    def test_duplicate_zip_rejected(self):
        with self.assertRaises(ValueError):
            r.safe_zip(zipped([('a', b'x'), ('a', b'y')]))

    def test_empty_zip_rejected(self):
        with self.assertRaises(ValueError):
            r.safe_zip(zipped([]))

    def test_symlink_zip_rejected(self):
        info = zipfile.ZipInfo('link')
        info.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            r.safe_zip(zipped([(info, b'target')]))

    def test_outer_digest_rejected_before_parsing(self):
        with self.assertRaisesRegex(ValueError, 'digest'):
            r.validate_artifact({}, {'size_in_bytes': 4, 'digest': 'sha256:bad'}, b'abcd')

    def test_existing_tag_blocks_all_writes(self):
        class API:
            def absent(self, path):
                raise ValueError('existing tag')
            def request(self, *args, **kwargs):
                raise AssertionError('must not write')
        with self.assertRaisesRegex(ValueError, 'existing tag'):
            r.publish({'tag': 'v0.1.0'}, API(), {})

    def test_existing_draft_blocks_all_writes(self):
        class API:
            def absent(self, path):
                pass
            def request(self, path, method='GET', *args, **kwargs):
                if method != 'GET':
                    raise AssertionError('must not write')
                return [{'tag_name': 'v0.1.0', 'draft': True}]
        with self.assertRaisesRegex(ValueError, 'existing draft'):
            r.publish({'tag': 'v0.1.0'}, API(), {})

    def test_upload_mismatch_leaves_draft_unpublished(self):
        calls = []
        class API:
            def absent(self, path):
                pass
            def request(self, path, method='GET', data=None, **kwargs):
                calls.append((path, method, data))
                if path.startswith('/releases?'):
                    return []
                if path == '/git/refs':
                    return {}
                if path == '/releases':
                    self.created_draft = data['draft']
                    return {'id': 1}
                if method == 'POST' and '/assets?' in path:
                    return {'id': 2, 'name': 'RELEASE-v0.1.0.zh-CN.md', 'size': 5}
                if path == '/releases/assets/2':
                    return b'wrong'
                raise AssertionError('unexpected write')
        api = API()
        with self.assertRaisesRegex(ValueError, 'byte verification'):
            r.publish({'tag': 'v0.1.0', 'source_sha': 'fixed'}, api, {'RELEASE-v0.1.0.zh-CN.md': b'notes'})
        self.assertTrue(api.created_draft)
        self.assertFalse(any(m == 'PATCH' for _, m, _ in calls))

    def test_successful_publish_and_replacement_rejected(self):
        for replace in (False, True):
            calls = []
            class API:
                def absent(self, path):
                    pass
                def request(self, path, method='GET', data=None, **kwargs):
                    calls.append((path, method))
                    record = {'id': 2, 'name': 'RELEASE-v0.1.0.zh-CN.md', 'size': 5, 'digest': 'sha256:' + r.sha(b'notes')}
                    if path.startswith('/releases?'):
                        return []
                    if path == '/git/refs':
                        return {}
                    if path == '/releases':
                        return {'id': 1}
                    if '/assets?name=' in path:
                        return record
                    if path == '/releases/assets/2':
                        return b'notes'
                    if path.startswith('/releases/1/assets?'):
                        if replace:
                            record['id'] = 99
                        return [record]
                    if path == '/git/ref/tags/v0.1.0':
                        return {'object': {'type': 'commit', 'sha': 'fixed'}}
                    if path in ('/releases/1', '/releases/tags/v0.1.0'):
                        return {'id': 1, 'draft': False, 'prerelease': False, 'tag_name': 'v0.1.0', 'html_url': 'https://example.invalid/release'}
                    raise AssertionError(path)
            with self.subTest(replace=replace):
                if replace:
                    with self.assertRaisesRegex(ValueError, 'assets changed'):
                        r.publish({'tag': 'v0.1.0', 'source_sha': 'fixed'}, API(), {'RELEASE-v0.1.0.zh-CN.md': b'notes'})
                    self.assertFalse(any(m == 'PATCH' for _, m in calls))
                else:
                    r.publish({'tag': 'v0.1.0', 'source_sha': 'fixed'}, API(), {'RELEASE-v0.1.0.zh-CN.md': b'notes'})
                    self.assertEqual(sum(m == 'PATCH' for _, m in calls), 1)

    def test_workflow_permissions_and_checkout(self):
        text = (r.ROOT / '.github/workflows/release.yml').read_text()
        self.assertEqual(text.count('contents: write'), 1)
        self.assertIn('needs: validate', text)
        self.assertEqual(text.count('persist-credentials: false'), 2)
        self.assertNotIn('pull_request_target', text)
        self.assertNotIn('secrets.', text)
        self.assertIn('cancel-in-progress: false', text)


if __name__ == '__main__':
    unittest.main()
