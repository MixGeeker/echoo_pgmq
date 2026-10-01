"""Bounded four-arm factorial; three ordered blocks, fresh cluster per arm."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BASELINE = '115dcfe67c1dc2ebd2bfc004ed9ce0700947ce40'
WAKE_ONLY = '332c6afee1dab46132eabf77eacd42cd82305ec3'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-config',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    report={'schema':1,'baseline_sha':BASELINE,'candidate_sha':os.environ['GITHUB_SHA'],'wake_only_sha':WAKE_ONLY,
            'design':'ABCD/DCBA/BADC, fresh cluster per arm; A baseline B wake_only C nodelay_only D combined; default poll50ms',
            'runs':[],'status':'running'}
    def save(): (args.output/'pairs.json').write_text(json.dumps(report,indent=2))
    save()
    try:
        # Combined already passed the workflow's ordinary gate. Baseline and
        # immutable wake-only have recorded ordinary CI; gate NODELAY-only too
        # before any new timed arm starts.
        install=['cmake','--install','build-nodelay','--config','RelWithDebInfo']
        if os.name!='nt':install.insert(0,'sudo')
        subprocess.run(install,cwd=ROOT,check=True)
        subprocess.run([sys.executable,'scripts/run_integration.py','--ordinary','--independent-client',
                        '--pg-config',args.pg_config,'--work-dir',str(args.output/'nodelay-ordinary-gate'),'--keep'],cwd=ROOT,check=True)
        report['nodelay_ordinary_gate']='passed';save()
        for pair,order in enumerate((('baseline','wake_only','nodelay_only','combined'),
                                     ('combined','nodelay_only','wake_only','baseline'),
                                     ('wake_only','baseline','combined','nodelay_only')),1):
            for position,variant in enumerate(order,1):
                directory=args.output/f'pair-{pair}-{position}-{variant}'
                row={'pair':pair,'position':position,'variant':variant,'status':'running','directory':directory.name}
                report['runs'].append(row);save()
                build={'baseline':'build-baseline','wake_only':'build-wake','nodelay_only':'build-nodelay','combined':'build'}[variant]
                command=['cmake','--install',build,'--config','RelWithDebInfo']
                if os.name!='nt':command.insert(0,'sudo')
                subprocess.run(command,cwd=ROOT,check=True)
                pkglib=Path(subprocess.check_output([args.pg_config,'--pkglibdir'],text=True).strip())
                module=pkglib/('echoo_pgmq.dll' if os.name=='nt' else 'echoo_pgmq.so')
                row['module_sha256']=hashlib.sha256(module.read_bytes()).hexdigest();save()
                process=subprocess.run([sys.executable,'scripts/run_integration.py','--wakeup-benchmark','--pg-config',args.pg_config,'--work-dir',str(directory),'--keep'],cwd=ROOT)
                row['returncode']=process.returncode
                row['status']='passed' if process.returncode==0 else 'failed';save()
                process.check_returncode()
        report['status']='passed';save()
    except BaseException as error:
        report['status']='failed';report['error']=repr(error);save();raise


if __name__=='__main__':main()
