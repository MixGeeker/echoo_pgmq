"""Bounded four-arm factorial; three ordered blocks, fresh cluster per arm."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from wakeup_install import assert_stopped, install, preflight

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
        records=preflight(ROOT)
        report['expected_modules']=records
        report['module_mode']='exact unmodified build bytes; ordinary package tests separate'
        save()
        assert_stopped(args.pg_config,Path(os.environ['RUNNER_TEMP'])/'evidence')
        # Combined already passed the workflow's ordinary gate. Baseline and
        # immutable wake-only have recorded ordinary CI; gate NODELAY-only too
        # before any new timed arm starts.
        report['nodelay_gate_module_sha256']=install(ROOT,args.pg_config,records['nodelay_only']);save()
        subprocess.run([sys.executable,'scripts/run_integration.py','--ordinary','--independent-client',
                        '--pg-config',args.pg_config,'--work-dir',str(args.output/'nodelay-ordinary-gate'),'--keep'],cwd=ROOT,check=True)
        assert_stopped(args.pg_config,args.output/'nodelay-ordinary-gate')
        report['nodelay_ordinary_gate']='passed';save()
        for pair,order in enumerate((('baseline','wake_only','nodelay_only','combined'),
                                     ('combined','nodelay_only','wake_only','baseline'),
                                     ('wake_only','baseline','combined','nodelay_only')),1):
            for position,variant in enumerate(order,1):
                directory=args.output/f'pair-{pair}-{position}-{variant}'
                row={'pair':pair,'position':position,'variant':variant,'status':'running','directory':directory.name}
                report['runs'].append(row);save()
                row['module_sha256']=install(ROOT,args.pg_config,records[variant]);save()
                process=subprocess.run([sys.executable,'scripts/run_integration.py','--wakeup-benchmark','--pg-config',args.pg_config,'--work-dir',str(directory),'--keep'],cwd=ROOT)
                assert_stopped(args.pg_config,directory)
                row['cluster_stopped']=True
                row['returncode']=process.returncode
                row['status']='passed' if process.returncode==0 else 'failed';save()
                process.check_returncode()
        report['status']='passed';save()
    except BaseException as error:
        report['status']='failed';report['error']=repr(error);save();raise


if __name__=='__main__':main()
