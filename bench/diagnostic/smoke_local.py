#!/usr/bin/env python3
"""0.2s warmup + 1s normal fullpath per arm. Infrastructure only; /tmp is tmpfs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pg-install',type=Path,required=True)
    p.add_argument('--build-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=False)
    results=[]
    for arm in ('A','B'):
        with tempfile.TemporaryDirectory(prefix='echoo-tail-smoke-') as temp:
            root=Path(temp); pg=root/'pg'; state=root/'state'; state.mkdir()
            shutil.copytree(a.pg_install,pg,symlinks=True)
            binary=pg/'lib/postgresql/echoo_pgmq.so'
            if binary.exists() or binary.is_symlink(): binary.unlink()
            binary.symlink_to(state/'selected/echoo_pgmq.so')
            for f in (a.build_root/arm/'sql').glob('*.sql'): shutil.copy2(f,pg/'share/postgresql/extension'/f.name)
            shutil.copy2(a.build_root/arm/'echoo_pgmq.control',pg/'share/postgresql/extension/echoo_pgmq.control')
            arms=root/'arms'/arm; arms.mkdir(parents=True)
            shutil.copy2(a.build_root/('build-'+arm)/'echoo_pgmq.so',arms/'echoo_pgmq.so')
            env=dict(os.environ,ECHOO_DIAGNOSTIC_STATE=str(state),ECHOO_DIAGNOSTIC_PG=str(pg/'bin'),
                     ECHOO_DIAGNOSTIC_ARMS=str(root/'arms'),ECHOO_DIAGNOSTIC_ARM=arm,
                     LD_LIBRARY_PATH='/workspace/shared/toolchain/proton/lib')
            with (a.output/(arm+'-server.log')).open('w') as log:
                server=subprocess.Popen([sys.executable,HERE/'server.py','echoo'],env=env,stdout=log,stderr=subprocess.STDOUT)
                error=None
                try:
                    end=time.monotonic()+20
                    while not (state/'ready.json').exists():
                        if server.poll() is not None: raise RuntimeError('Smoke stack exited before ready')
                        if time.monotonic()>=end: raise TimeoutError('Smoke stack readiness')
                        time.sleep(.1)
                    shutil.copy2(state/'ready.json',a.output/(arm+'-ready.json'))
                    args=[sys.executable,HERE/'client.py','--label',arm,'--output',a.output,
                          '--dsn','host=127.0.0.1 port=5432 user=bench_admin dbname=postgres connect_timeout=3',
                          '--url','amqps://localhost:5671','--ca',state/'certs/ca.pem','--cert',state/'certs/client.pem',
                          '--key',state/'certs/client.key','--warmup-seconds',.2,'--duration-seconds',1,
                          '--offered-rate',200,'--drain-seconds',3,'--timeout',3]
                    with (a.output/(arm+'-client.log')).open('w') as clientlog:
                        subprocess.run(list(map(str,args)),check=True,env=env,stdout=clientlog,stderr=subprocess.STDOUT,timeout=12)
                except Exception as exc:
                    error=repr(exc)
                finally:
                    server.send_signal(signal.SIGTERM)
                    server.wait(timeout=15)
                    if (state/'trace').exists(): shutil.copytree(state/'trace',a.output/(arm+'-trace'))
                    if (state/'postgres.log').exists(): shutil.copy2(state/'postgres.log',a.output/(arm+'-postgres.log'))
                results.append({'arm':arm,'error':error,'server_exit':server.returncode})
                if error: break
    (a.output/'validation.json').write_text(json.dumps({'evidence_kind':'local ordinary fullpath smoke only',
        'storage':'/tmp is tmpfs, NOT disk persistence or performance evidence',
        'cgroup_budget':'not exercised locally; Docker HTTP/cgroup path is unrun here',
        'load':'0.2 seconds warmup + 1 second measured per arm; never PG gate or A/B conclusion',
        'results':results},indent=2)+'\n')
    return any(r['error'] or r['server_exit'] for r in results)

if __name__=='__main__': raise SystemExit(main())
