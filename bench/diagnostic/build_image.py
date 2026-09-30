#!/usr/bin/env python3
"""One build, bounded separately from the 600-second measurement controller."""
import argparse
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
from instrument_native import materialize
from policy import schedule


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tag',required=True); p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=False)
    def deadline(*_): raise TimeoutError('900-second image build budget expired')
    signal.signal(signal.SIGALRM,deadline); signal.alarm(900)
    started=time.monotonic(); result={'build_budget_seconds':900,'excluded_from_measurement':True,
        'started_unix_seconds':time.time(),'tag':a.tag,'automatic_retries':0}
    try:
        if (HERE/'generated').exists(): raise ValueError('Refusing an existing generated build context')
        with tempfile.TemporaryDirectory(prefix='echoo-diag-build-') as temp:
            generated=Path(temp)/'source'
            manifest=materialize(REPO,generated)
            shutil.copytree(generated,HERE/'generated')
            (a.output/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
            with (a.output/'build.log').open('w') as log:
                subprocess.run(['docker','build','--pull','-f',str(HERE/'Dockerfile'),'-t',a.tag,str(REPO)],
                               check=True,stdout=log,stderr=subprocess.STDOUT,timeout=max(1,890-(time.monotonic()-started)))
            result['image_id']=subprocess.check_output(['docker','image','inspect',a.tag,'--format={{.Id}}'],text=True,timeout=5).strip()
            result['status']='built'
    except BaseException as error:
        result.update(status='build_insufficient',error=repr(error),unrun_units=schedule()['units'])
    finally:
        result['elapsed_seconds']=time.monotonic()-started
        (a.output/'build-status.json').write_text(json.dumps(result,indent=2)+'\n')
    signal.alarm(0)
    return result['status']!='built'

if __name__=='__main__': raise SystemExit(main())
