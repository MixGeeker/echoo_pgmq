import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('install',Path(__file__).with_name('wakeup_install.py'))
install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)


class InstallTests(unittest.TestCase):
    def test_four_distinct_hashes_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i,build in enumerate(install.BUILDS.values()):
                p=root/build/('RelWithDebInfo/echoo_pgmq.dll' if install.os.name=='nt' else 'echoo_pgmq.so')
                p.parent.mkdir(parents=True);p.write_bytes(str(i).encode())
            records=install.preflight(root);self.assertEqual(len(records),4)
            Path(records['combined']['source']).write_bytes(b'0')
            with self.assertRaisesRegex(ValueError,'four distinct'):install.preflight(root)
    def test_running_or_unknown_cluster_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'data').mkdir()
            for code in (0,1,4):
                with patch.object(install.subprocess,'check_output',return_value='/tmp/bin'),patch.object(install.subprocess,'run',return_value=subprocess.CompletedProcess([],code)):
                    with self.subTest(code=code),self.assertRaises(RuntimeError):install.assert_stopped('pg_config',tmp)
            with patch.object(install.subprocess,'check_output',return_value='/tmp/bin'),patch.object(install.subprocess,'run',return_value=subprocess.CompletedProcess([],3)):
                install.assert_stopped('pg_config',tmp)
    def test_changed_build_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'module';p.write_bytes(b'changed')
            with patch.object(install.subprocess,'run') as run:
                with self.assertRaisesRegex(ValueError,'changed after preflight'):
                    install.install(tmp,'pg_config',{'source':str(p),'sha256':'wrong','build':'build'})
                run.assert_not_called()
    def test_exact_staged_and_installed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'build-module';source.write_bytes(b'new')
            dest=root/('echoo_pgmq.dll' if install.os.name=='nt' else 'echoo_pgmq.so');dest.write_bytes(b'stale')
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if 'copy' in args:Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
                if 'rename' in args:Path(args[-2]).replace(args[-1])
                return subprocess.CompletedProcess(args,0)
            with patch.object(install.subprocess,'check_output',return_value=str(root)),patch.object(install.subprocess,'run',side_effect=run):
                result=install.install(root,'pg_config',{'source':str(source),'sha256':install.digest(source),'build':'build'})
            self.assertEqual(result,install.digest(source));self.assertEqual(dest.read_bytes(),b'new')
            self.assertEqual(len(calls),3)


if __name__=='__main__':unittest.main()
