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
            if isinstance(name, str):
                # ZipInfo turns os.sep into '/' on Windows; keep the raw member
                # name so unsafe-name rejection is exercised on every platform.
                info = zipfile.ZipInfo(name)
                info.filename = name
                name = info
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

    def test_new_release_separates_distribution_and_sql(self):
        config = {'repository':'MixGeeker/echoo_pgmq','tag':'v0.1.1',
                  'distribution_version':'0.1.1','native_build_version':'0.1.1',
                  'sql_default_version':'0.1.0','extensionVersion':'0.1.0',
                  'sql_available_versions':['0.1.0','0.1.1'],
                  'expected_tests_per_phase':{'16':47,'17':47,'18':55},
                  'user_reported_testing':'not yet performed for v0.1.1',
                  'artifacts':[{'id':i,'name':str(i)} for i in range(12)]}
        r.validate_config(config)
        for field,value in [('distribution_version','0.1.2'),('native_build_version','0.1.0'),
                            ('sql_default_version','0.1.1'),('extensionVersion','0.1.1'),
                            ('sql_available_versions',['0.1.1']),('user_reported_testing','passed')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                r.validate_config(dict(config, **{field:value}))
        self.assertEqual(4*sum(config['expected_tests_per_phase'].values()),596)

    def test_v012_adds_arm64_and_sql_012(self):
        config = {'repository':'MixGeeker/echoo_pgmq','tag':'v0.1.2',
                  'distribution_version':'0.1.2','native_build_version':'0.1.2',
                  'sql_default_version':'0.1.0','extensionVersion':'0.1.0',
                  'sql_available_versions':['0.1.0','0.1.1','0.1.2'],
                  'expected_tests_per_phase':{'16':51,'17':51,'18':59},
                  'user_reported_testing':'not yet performed for v0.1.2',
                  'artifacts':[{'id':i,'name':str(i)} for i in range(18)]}
        r.validate_config(config)
        for field,value in [('distribution_version','0.1.1'),('sql_default_version','0.1.2'),
                            ('sql_available_versions',['0.1.0','0.1.1']),
                            ('artifacts',[{'id':i,'name':str(i)} for i in range(12)])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                r.validate_config(dict(config, **{field:value}))

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

    def test_download_media_types_and_redirect_token_isolation(self):
        import urllib.error
        from unittest.mock import MagicMock
        for endpoint, accept in (('/actions/artifacts/123/zip', 'application/vnd.github+json'),
                                 ('/releases/assets/123', 'application/octet-stream')):
            captured = []
            class Opener:
                def open(self, request, timeout):
                    captured.append(request)
                    raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://example.invalid/signed'}, None)
            response = MagicMock()
            response.read.return_value = b'zip-bytes'
            with patch.object(r.urllib.request, 'build_opener', return_value=Opener()), patch.object(r.urllib.request, 'urlopen', return_value=response) as redirected:
                result = r.GitHub('MixGeeker/echoo_pgmq', 'test-only-token').request(endpoint, binary=True)
            self.assertEqual(result, b'zip-bytes')
            self.assertEqual(captured[0].get_header('Accept'), accept)
            redirected.assert_called_once_with('https://example.invalid/signed', timeout=60)

    def test_candidate_architecture_follows_artifact_name(self):
        config = {'source_sha': 'a' * 40, 'tag': 'v0.1.0'}

        def artifact(name, architecture):
            archive = f'echoo-pgmq-0.1.0-candidate-pg18-linux-{architecture}.zip'
            manifest = {'commit': config['source_sha'], 'version': '0.1.0', 'status': 'candidate-not-production-release',
                        'qualification_status': 'blocked_security_review', 'project_license': 'Apache-2.0',
                        'system': 'Linux', 'postgres_build': 'PostgreSQL 18.0', 'architecture': architecture,
                        'files_sha256': {'missing': '0'}}
            raw = zipped([('root/MANIFEST.json', json.dumps(manifest))])
            outer = zipped([(archive, raw), (archive + '.sha256', f'{r.sha(raw)}  {archive}\n')])
            item = {'name': name, 'size_in_bytes': len(outer), 'digest': 'sha256:' + r.sha(outer)}
            return item, outer

        accepted = [('linux-pg18-candidate', 'x86_64'), ('linux-arm64-pg18-candidate', 'aarch64')]
        rejected = [('linux-pg18-candidate', 'aarch64'), ('linux-arm64-pg18-candidate', 'x86_64')]
        for name, architecture in accepted:
            with self.subTest(name=name, architecture=architecture), self.assertRaisesRegex(ValueError, 'inner file set'):
                r.validate_artifact(config, *artifact(name, architecture))
        for name, architecture in rejected:
            with self.subTest(name=name, architecture=architecture), self.assertRaisesRegex(ValueError, 'unexpected architecture'):
                r.validate_artifact(config, *artifact(name, architecture))

    def test_ci_builds_linux_arm64_natively(self):
        text = (r.ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn("arch: ['x64', 'arm64']", text)
        self.assertIn("'ubuntu-24.04-arm'", text)
        self.assertIn("linux${{ matrix.arch == 'arm64' && '-arm64' || '' }}-pg${{ matrix.pg }}", text)

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
