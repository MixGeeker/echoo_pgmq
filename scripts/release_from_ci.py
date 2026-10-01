#!/usr/bin/env python3
"""Publish explicitly pinned, tested CI bytes without rebuilding or overwriting."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token):
        self.base = 'https://api.github.com/repos/' + repository
        self.token = token

    def request(self, path, method='GET', data=None, binary=False, upload=False):
        host = 'https://uploads.github.com/repos/' if upload else None
        url = (host + self.base.split('/repos/')[1] + path) if host else self.base + path
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/octet-stream' if binary and path.startswith('/releases/assets/') else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'echoo-release-ci'}
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if upload else 'application/json'
            if not upload:
                data = json.dumps(data).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = urllib.request.build_opener(NoRedirect).open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308) or not binary or method != 'GET':
                raise
            # Signed download URLs receive NO GitHub authorization headers.
            target = e.headers['Location']
            require(urllib.parse.urlsplit(target).scheme == 'https', 'unsafe download redirect')
            response = urllib.request.urlopen(target, timeout=60)
        with response:
            payload = response.read()
        return payload if binary else json.loads(payload)

    def absent(self, path):
        try:
            self.request(path)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return
            raise
        raise ValueError('Refusing to overwrite existing tag/release: ' + path)


def safe_zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    require(names and len(names) == len(set(names)), 'empty/duplicate ZIP members')
    for item in z.infolist():
        p = PurePosixPath(item.filename)
        require(not p.is_absolute() and '..' not in p.parts and '\\' not in item.filename
                and ':' not in item.filename and not item.is_dir()
                and item.orig_filename == item.filename, 'unsafe ZIP member')
        require((item.external_attr >> 16) & 0o170000 != 0o120000, 'symlink in ZIP')
    require(z.testzip() is None, 'ZIP CRC failure')
    return z


def source_bytes(config, path):
    return subprocess.check_output(['git', 'show', config['source_sha'] + ':' + path], cwd=ROOT)


def validate_artifact(config, item, data):
    require(len(data) == item['size_in_bytes'] and 'sha256:' + sha(data) == item['digest'],
            'pinned outer artifact digest mismatch')
    z = safe_zip(data)
    name = item['name']
    pg = re.search(r'-pg(16|17|18)-', name).group(1)
    if name.endswith('-candidate'):
        archives = [n for n in z.namelist() if n.endswith('.zip')]
        require(len(archives) == 1, 'one candidate ZIP required')
        archive = archives[0]
        require(PurePosixPath(archive).name == archive, 'nested candidate filename')
        raw = z.read(archive)
        require(z.read(archive + '.sha256').decode().split() == [sha(raw), archive], 'candidate checksum mismatch')
        inner = safe_zip(raw)
        manifests = [n for n in inner.namelist() if n.endswith('/MANIFEST.json')]
        require(len(manifests) == 1, 'one manifest required')
        prefix = manifests[0][:-len('MANIFEST.json')]
        require(all(n.startswith(prefix) for n in inner.namelist()), 'multiple candidate roots')
        m = json.loads(inner.read(manifests[0]))
        require(m['commit'] == config['source_sha'] and m['version'] == config.get('distribution_version', '0.1.0'), 'source/version mismatch')
        if config['tag'] == 'v0.1.1':
            for field in ('distribution_version', 'native_build_version', 'sql_default_version', 'sql_available_versions', 'extensionVersion'):
                require(m.get(field) == config[field], 'version identity mismatch: ' + field)
        require(m['status'] == 'candidate-not-production-release' and m['qualification_status'] == 'blocked_security_review', 'qualification metadata changed')
        require(m['project_license'] == 'Apache-2.0', 'project license mismatch')
        system = 'Windows' if name.startswith('native-windows-') else 'Linux'
        require(m['system'] == system and m['postgres_build'].split()[1].split('.')[0] == pg, 'platform mismatch')
        require(m['architecture'].lower() in ('amd64', 'x86_64'), 'unexpected architecture')
        require(archive == f"echoo-pgmq-{m['version']}-candidate-pg{pg}-{system.lower()}-{m['architecture'].lower()}.zip", 'candidate filename/version mismatch')
        actual = {n[len(prefix):]: sha(inner.read(n)) for n in inner.namelist() if n != manifests[0]}
        require(actual == m['files_sha256'], 'inner file set/hash mismatch')
        for filename in ('LICENSE', 'NOTICE'):
            require(inner.read(prefix + filename) == source_bytes(config, filename), 'project notice differs from source')
        require(b'MixGeeker' in inner.read(prefix + 'NOTICE'), 'missing attribution')
        for filename, digest in config['proton_notices_sha256'].items():
            require(sha(inner.read(prefix + 'third-party/' + filename)) == digest, 'Proton notice mismatch')
        if system == 'Windows':
            require(m['proton_windows_openssl_patch'] is True, 'missing Windows patch identity')
            require(b'Original Apache-2.0 license and notices remain' in inner.read(prefix + 'third-party/proton-ECHOO_WINDOWS_OPENSSL_PATCH.txt'), 'missing patch notice')
        require(inner.read(prefix + 'share/extension/echoo_pgmq.control').replace(b'\r\n', b'\n') == source_bytes(config, 'echoo_pgmq.control'), 'SQL default mismatch')
        return {archive: raw, archive + '.sha256': z.read(archive + '.sha256')}, 0
    require(name.endswith('-evidence'), 'unexpected artifact class')
    expected = config.get('expected_tests_per_phase', {'16': 37, '17': 37, '18': 45})[pg]
    for phase in ('evidence', 'candidate-evidence'):
        xml = ET.fromstring(z.read(phase + '/junit.xml'))
        suites = xml.findall('testsuite')
        require(len(suites) == 1, 'one test suite required')
        suite = suites[0]
        require(int(suite.attrib['tests']) == expected, 'test count changed')
        require(all(int(suite.attrib[k]) == 0 for k in ('failures', 'errors', 'skipped')), 'non-passing test suite')
        cases = suite.findall('testcase')
        require(len(cases) == expected and all(not any(c.find(k) is not None for k in ('failure', 'error', 'skipped')) for c in cases), 'non-passing testcase')
        require(len({(c.attrib.get('classname'), c.attrib['name']) for c in cases}) == expected, 'duplicate testcase')
        require(sum(c.attrib['name'] == 'test_candidate_preserves_project_and_dependency_notices' for c in cases) == 1, 'notice regression absent')
        env = json.loads(z.read(phase + '/environment.json'))
        require(env['test_scope'] == 'ordinary-regression' and env['process_crash_tests_enabled'] is False
                and env['physical_power_loss_tested'] is False and env['qualification_status'] == 'blocked_security_review'
                and env['sql_scripts_passed'] == ['core.sql'], 'evidence scope mismatch')
        require('database system is shut down' in z.read(phase + '/postgres.log').decode(), 'unclean test shutdown')
    return {name + '.zip': data}, expected * 2


def validate_config(config):
    require(config['repository'] == 'MixGeeker/echoo_pgmq' and config['tag'] in ('v0.1.0', 'v0.1.1'), 'release scope changed')
    if config['tag'] == 'v0.1.1':
        require(config['distribution_version'] == config['native_build_version'] == '0.1.1', 'distribution/native version mismatch')
        require(config['extensionVersion'] == config['sql_default_version'] == '0.1.0' and config['sql_available_versions'] == ['0.1.0', '0.1.1'], 'SQL migration/default changed')
        counts = config['expected_tests_per_phase']
        require(set(counts) == {'16', '17', '18'} and all(isinstance(v, int) and v > 0 for v in counts.values()), 'exact test matrix required')
        require(config['user_reported_testing'] == 'not yet performed for v0.1.1', 'unsupported user-testing claim')
    require(len(config['artifacts']) == 12 and len({a['id'] for a in config['artifacts']}) == 12 and len({a['name'] for a in config['artifacts']}) == 12, 'artifact identities incomplete')


def prepare(config, api, output):
    validate_config(config)
    head = os.environ['GITHUB_SHA']
    require(os.environ['GITHUB_REF'] == 'refs/heads/main' and os.environ['GITHUB_REPOSITORY'] == config['repository'], 'trusted main required')
    require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == head, 'automation checkout mismatch')
    subprocess.run(['git', 'merge-base', '--is-ancestor', config['source_sha'], head], cwd=ROOT, check=True)
    require(subprocess.check_output(['git', 'rev-parse', config['source_sha'] + '^{tree}'], cwd=ROOT, text=True).strip() == config['source_tree'], 'source tree mismatch')
    if config['tag'] == 'v0.1.1':
        identity = json.loads(source_bytes(config, 'package_versions.json'))
        require(all(identity[k] == config[k] for k in ('distribution_version', 'native_build_version', 'sql_default_version', 'sql_available_versions', 'extensionVersion')), 'source version identity mismatch')
    run = api.request('/actions/runs/' + str(config['ci_run_id']))
    require(run['head_sha'] == config['source_sha'] and run['head_branch'] == 'main' and run['event'] == 'push'
            and run['repository']['id'] == config['repository_id'] and run['head_repository']['id'] == config['repository_id']
            and run['status'] == 'completed' and run['conclusion'] == 'success'
            and run['run_attempt'] == config['ci_run_attempt'] and run['path'] == '.github/workflows/ci.yml', 'untrusted CI run')
    jobs = api.request('/actions/runs/' + str(config['ci_run_id']) + '/attempts/' + str(config['ci_run_attempt']) + '/jobs?per_page=100')
    expected_jobs = {f'Linux PG{p} / native AMQP 1.0' for p in (16, 17, 18)} | {f'Native Windows Server 2022 PG{p} (not Windows 11 certification)' for p in (16, 17, 18)}
    require(jobs['total_count'] == 6 and {j['name'] for j in jobs['jobs']} == expected_jobs
            and all(j['conclusion'] == 'success' and j['head_sha'] == config['source_sha'] for j in jobs['jobs']), 'CI matrix incomplete')
    artifacts = api.request('/actions/runs/' + str(config['ci_run_id']) + '/artifacts?per_page=100')
    require(artifacts['total_count'] == 12 and len(artifacts['artifacts']) == 12, 'artifact count changed')
    live = {a['id']: a for a in artifacts['artifacts']}
    assets = {}
    tests = 0
    for item in config['artifacts']:
        a = live[item['id']]
        require(all(a[k] == item[k] for k in ('id', 'name', 'size_in_bytes', 'digest')) and not a['expired'], 'artifact metadata changed')
        require(a['workflow_run']['id'] == config['ci_run_id'] and a['workflow_run']['head_sha'] == config['source_sha'], 'artifact run mismatch')
        raw = api.request('/actions/artifacts/' + str(a['id']) + '/zip', binary=True)
        verified, count = validate_artifact(config, item, raw)
        require(not (assets.keys() & verified.keys()), 'duplicate release asset')
        assets.update(verified)
        tests += count
    expected_total = 4 * sum(config.get('expected_tests_per_phase', {'16': 37, '17': 37, '18': 45}).values())
    require(tests == expected_total and len(assets) == 18, 'release coverage changed')
    tag = config['tag']
    assets[f'echoo-pgmq-{tag}-source.zip'] = subprocess.check_output(['git', 'archive', '--format=zip', f'--prefix=echoo-pgmq-{tag}/', config['source_sha']], cwd=ROOT)
    binary_manifest = []
    for name, data in sorted(assets.items()):
        if name.endswith('.zip') and '-candidate-pg' in name:
            inner = safe_zip(data)
            manifest_path = next(n for n in inner.namelist() if n.endswith('/MANIFEST.json'))
            binary_manifest.append({'filename': name, 'sha256': sha(data),
                'url': 'https://github.com/' + config['repository'] + '/releases/download/' + config['tag'] + '/' + name,
                'original_manifest': json.loads(inner.read(manifest_path))})
    assets[f'BINARY-MANIFEST-{tag}.json'] = (json.dumps(binary_manifest, indent=2) + '\n').encode()
    assets['install_candidate.py'] = source_bytes(config, 'scripts/install_candidate.py')
    assets[f'RELEASE-{tag}.zh-CN.md'] = (ROOT / f'docs/releases/{tag}.md').read_bytes()
    provenance = {'release': config['tag'], 'source_commit': config['source_sha'], 'source_tree': config['source_tree'],
                  'release_automation_commit': head, 'release_workflow_run': os.environ['GITHUB_RUN_ID'],
                  'ci_run_id': config['ci_run_id'], 'ci_run_attempt': config['ci_run_attempt'],
                  'ci_url': run['html_url'], 'ordinary_test_executions': tests, 'core_sql_passes': 12,
                  'qualification_status': 'blocked_security_review', 'user_reported_testing': config.get('user_reported_testing', 'passed; scope unspecified'),
                  'signature': 'unsigned; checksums and CI provenance are not a signature or security certification',
                  'original_ci_artifacts': config['artifacts'],
                  'assets_sha256': {n: sha(d) for n, d in sorted(assets.items())}}
    provenance['version_identity'] = {k: config.get(k) for k in ('distribution_version', 'native_build_version', 'sql_default_version', 'sql_available_versions', 'extensionVersion')}
    assets[f'PROVENANCE-{tag}.json'] = (json.dumps(provenance, indent=2) + '\n').encode()
    assets['SHA256SUMS'] = ''.join(f'{sha(d)}  {n}\n' for n, d in sorted(assets.items())).encode()
    output.mkdir(parents=True, exist_ok=False)
    for n, data in assets.items():
        (output / n).write_bytes(data)
    print(json.dumps({'validated_assets': len(assets), 'tests': tests, 'source': config['source_sha']}))
    return assets


def publish(config, api, assets):
    tag = config['tag']
    api.absent('/git/ref/tags/' + tag)
    api.absent('/releases/tags/' + tag)
    # A draft is not returned by releases/tags. Include it in collision checks.
    page = 1
    while True:
        releases = api.request('/releases?per_page=100&page=' + str(page))
        require(all(r['tag_name'] != tag for r in releases), 'existing draft/release; manual recovery required')
        if len(releases) < 100:
            break
        page += 1
    api.request('/git/refs', 'POST', {'ref': 'refs/tags/' + tag, 'sha': config['source_sha']})
    release = api.request('/releases', 'POST', {'tag_name': tag, 'target_commitish': config['source_sha'],
        'name': 'Echoo PGMQ ' + tag, 'body': assets[f'RELEASE-{tag}.zh-CN.md'].decode(),
        'draft': True, 'prerelease': False, 'make_latest': 'false'})
    rid = release['id']
    uploaded = {}
    for name, data in sorted(assets.items()):
        result = api.request('/releases/' + str(rid) + '/assets?name=' + urllib.parse.quote(name), 'POST', data, upload=True)
        require(result['name'] == name and result['size'] == len(data), 'uploaded asset metadata mismatch')
        downloaded = api.request('/releases/assets/' + str(result['id']), binary=True)
        require(sha(downloaded) == sha(data), 'uploaded asset byte verification failed')
        uploaded[name] = (result['id'], result['size'], 'sha256:' + sha(data))
    actual = api.request('/releases/' + str(rid) + '/assets?per_page=100')
    require({a['name']: (a['id'], a['size'], a['digest']) for a in actual} == uploaded and len(actual) == len(assets), 'release assets changed')
    ref = api.request('/git/ref/tags/' + tag)
    require(ref['object']['type'] == 'commit' and ref['object']['sha'] == config['source_sha'], 'tag changed before publication')
    final = api.request('/releases/' + str(rid), 'PATCH', {'draft': False, 'make_latest': 'true'})
    require(final['draft'] is False and final['prerelease'] is False and final['tag_name'] == tag, 'release did not publish')
    public = api.request('/releases/tags/' + tag)
    require(public['id'] == rid and public['draft'] is False, 'public release readback failed')
    final_assets = api.request('/releases/' + str(rid) + '/assets?per_page=100')
    require({a['name']: (a['id'], a['size'], a['digest']) for a in final_assets} == uploaded, 'published assets differ')
    print('Published and byte-verified: ' + final['html_url'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--config', choices=('releases/v0.1.0.json', 'releases/v0.1.1.json'), default='releases/v0.1.1.json')
    args = parser.parse_args()
    config = json.loads((ROOT / args.config).read_text())
    api = GitHub(config['repository'], os.environ['GH_TOKEN'])
    assets = prepare(config, api, args.output)
    if args.publish:
        publish(config, api, assets)


if __name__ == '__main__':
    main()
