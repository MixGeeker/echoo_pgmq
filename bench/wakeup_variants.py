"""Construct exactly baseline+socket patch; assert its relation to combined source."""
import hashlib
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASELINE='115dcfe67c1dc2ebd2bfc004ed9ce0700947ce40'
WAKE='332c6afee1dab46132eabf77eacd42cd82305ec3'


def main():
    for directory,expected in [('baseline-source',BASELINE),('wake-source',WAKE)]:
        actual=subprocess.check_output(['git','-C',str(ROOT/directory),'rev-parse','HEAD'],text=True).strip()
        assert actual==expected,(directory,actual)
    target=ROOT/'nodelay-source'
    if target.exists():raise ValueError('refuse existing derived source')
    shutil.copytree(ROOT/'baseline-source',target,ignore=shutil.ignore_patterns('.git'))
    patch=ROOT/'bench/experiments/server-nodelay.patch'
    subprocess.run(['git','apply','--unidiff-zero','--check','--directory=nodelay-source',str(patch)],cwd=ROOT,check=True)
    subprocess.run(['git','apply','--unidiff-zero','--directory=nodelay-source',str(patch)],cwd=ROOT,check=True)
    baseline=(ROOT/'baseline-source/src/echoo_pgmq.c').read_text()
    wake=(ROOT/'wake-source/src/echoo_pgmq.c').read_text()
    combined=(ROOT/'src/echoo_pgmq.c').read_text()
    nodelay=(target/'src/echoo_pgmq.c').read_text()
    start=wake.index('/* Only the AMQP worker calls this,')
    end=wake.index('static void\nreceive_message(',start)
    wakeblock=wake[start:end]
    wakecall='        invalidate_empty_consumers(link->queue);\n'
    assert wake.replace(wakeblock,'').replace(wakecall,'')==baseline
    assert combined.replace(wakeblock,'').replace(wakecall,'')==nodelay
    for name,text in [('baseline',baseline),('wake_only',wake),('nodelay_only',nodelay),('combined',combined)]:
        print(name,hashlib.sha256(text.encode()).hexdigest())


if __name__=='__main__':main()
