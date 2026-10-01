"""Six fresh clusters, alternating AB/BA/AB, with exact immutable build inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BASELINE = '115dcfe67c1dc2ebd2bfc004ed9ce0700947ce40'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-config',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    report={'schema':1,'baseline_sha':BASELINE,'candidate_sha':os.environ['GITHUB_SHA'],
            'design':'AB/BA/AB, fresh cluster per arm; default poll50ms; identical dependencies and durability',
            'runs':[],'status':'running'}
    def save(): (args.output/'pairs.json').write_text(json.dumps(report,indent=2))
    save()
    try:
        for pair,order in enumerate((('baseline','candidate'),('candidate','baseline'),('baseline','candidate')),1):
            for position,variant in enumerate(order,1):
                directory=args.output/f'pair-{pair}-{position}-{variant}'
                row={'pair':pair,'position':position,'variant':variant,'status':'running','directory':directory.name}
                report['runs'].append(row);save()
                command=['cmake','--install','build-baseline' if variant=='baseline' else 'build','--config','RelWithDebInfo']
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
