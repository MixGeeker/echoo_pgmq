"""Ordinary packaging compatibility: distribution identity is not SQL migration."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
packager=load('package_candidate');installer=load('install_candidate')


def fixture(root,new):
    suffix='.dll' if os.name=='nt' else '.so'
    files={'lib/echoo_pgmq'+suffix:b'inert native fixture',
           'share/extension/echoo_pgmq.control':b"default_version = '0.1.0'\n",
           'share/extension/echoo_pgmq--0.1.0.sql':b'-- default fixture',
           'share/extension/echoo_pgmq--0.1.1.sql':b'-- optional fixture',
           'share/extension/echoo_pgmq--0.1.0--0.1.1.sql':b'-- explicit upgrade fixture',
           ('runtime-bin/qpid-proton.dll' if os.name=='nt' else 'lib/libqpid-proton.so'):b'inert dependency'}
    manifest={'status':'candidate-not-production-release','version':'0.1.1' if new else '0.1.0',
              'postgres_build':'PostgreSQL 18.6','system':platform.system(),'architecture':platform.machine(),
              'files_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    if new:manifest.update(json.loads((ROOT/'package_versions.json').read_text()))
    archive=root/('new.zip' if new else 'old.zip')
    with zipfile.ZipFile(archive,'w') as out:
        for name,data in files.items():out.writestr('candidate/'+name,data)
        out.writestr('candidate/MANIFEST.json',json.dumps(manifest))
    archive.with_suffix('.zip.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    return archive


class VersionTests(unittest.TestCase):
    def test_source_build_and_sql_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/'CMakeCache.txt';cache.write_text('CMAKE_PROJECT_VERSION:STATIC=0.1.1\n')
            value=packager.version_identity(ROOT,cache)
            self.assertEqual(value['distribution_version'],'0.1.1')
            self.assertEqual(value['extensionVersion'],'0.1.0')
            self.assertEqual(value['sql_available_versions'],['0.1.0','0.1.1'])
    def test_stale_build_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/'CMakeCache.txt';cache.write_text('CMAKE_PROJECT_VERSION:STATIC=0.1.0\n')
            with self.assertRaisesRegex(ValueError,'native build version'):packager.version_identity(ROOT,cache)
    def test_installer_reads_old_and_new_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            for new in (False,True):
                manifest,_=installer.verified_members(fixture(Path(tmp),new))
                self.assertEqual(manifest['version'],'0.1.1' if new else '0.1.0')
    def test_install_copies_optional_sql_without_running_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=fixture(root,True);calls=[]
            def pg_value(args,**kwargs):
                calls.append(args[1]);return {'--version':'PostgreSQL 18.6','--pkglibdir':str(root/'lib'),'--sharedir':str(root/'share'),'--bindir':str(root/'bin')}[args[1]]
            with patch.object(installer.subprocess,'check_output',side_effect=pg_value):installer.install(archive,'fixture-pg-config')
            self.assertTrue((root/'share/extension/echoo_pgmq--0.1.0--0.1.1.sql').exists())
            self.assertEqual((root/'share/extension/echoo_pgmq.control').read_text(),"default_version = '0.1.0'\n")
            self.assertTrue(set(calls)<= {'--version','--pkglibdir','--sharedir','--bindir'})


if __name__=='__main__':unittest.main()
