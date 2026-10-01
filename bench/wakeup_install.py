"""Fail-closed arm identity and atomic module replacement in disposable CI only."""
import hashlib
import os
from pathlib import Path
import subprocess

BUILDS={'baseline':'build-baseline','wake_only':'build-wake','nodelay_only':'build-nodelay','combined':'build'}


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(root):
    records={}
    for variant,build in BUILDS.items():
        source=Path(root)/build/('RelWithDebInfo/echoo_pgmq.dll' if os.name=='nt' else 'echoo_pgmq.so')
        if not source.is_file():raise ValueError(f'missing expected build module: {source}')
        records[variant]={'build':build,'source':str(source.resolve()),'sha256':digest(source)}
    if len({v['sha256'] for v in records.values()})!=len(BUILDS):
        raise ValueError('factorial requires four distinct built module hashes')
    return records


def assert_stopped(pg_config,cluster):
    data=Path(cluster)/'data'
    if not data.exists():return
    bindir=Path(subprocess.check_output([pg_config,'--bindir'],text=True).strip())
    pgctl=bindir/('pg_ctl.exe' if os.name=='nt' else 'pg_ctl')
    status=subprocess.run([str(pgctl),'-D',str(data),'status'],capture_output=True,text=True)
    if status.returncode!=3:
        raise RuntimeError(f'refusing module replacement: cluster is running or status unknown: {cluster}, rc={status.returncode}')


def install(root,pg_config,record):
    root=Path(root)
    def command(args):
        if os.name!='nt':args=['sudo',*args]
        subprocess.run(args,cwd=root,check=True)
    if digest(record['source'])!=record['sha256']:raise ValueError('build module changed after preflight')
    command(['cmake','--install',record['build'],'--config','RelWithDebInfo'])
    library=Path(subprocess.check_output([pg_config,'--pkglibdir'],text=True).strip())/('echoo_pgmq.dll' if os.name=='nt' else 'echoo_pgmq.so')
    stage=library.with_name(library.name+'.wakeup-stage')
    # CMake's install freshness heuristic may alias rapidly-built arms. Force
    # exact build bytes through a staged atomic rename; never truncate a mapped
    # library. All owned clusters must be proven stopped by the caller first.
    # These private CI builds retain their build RPATH to the same verified
    # Proton installation; normal package/reinstall tests run independently.
    command(['cmake','-E','copy',record['source'],str(stage)])
    if digest(stage)!=record['sha256']:raise ValueError('staged module hash mismatch')
    command(['cmake','-E','rename',str(stage),str(library)])
    installed=digest(library)
    if installed!=record['sha256']:raise ValueError('installed module differs from intended build')
    return installed
