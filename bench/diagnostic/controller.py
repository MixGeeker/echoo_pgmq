#!/usr/bin/env python3
"""ONE bounded diagnostic session. No retries or adaptive extra stages."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0,str(HERE.parent))
import compare_ci  # reuse real budget/settings verifier; never run its matrix
from policy import (schedule, State, gate_result, admit_unit, CONTROLLER_SECONDS,
                    WORK_SECONDS, WARMUP_SECONDS, GATE_SECONDS, CELL_SECONDS)


class BudgetExpired(RuntimeError): pass


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+"\n")


def raw_rows(path):
    with gzip.open(path,'rt') as stream:
        return [json.loads(line) for line in stream]


class Budget:
    def __init__(self):
        self.started=time.monotonic()
        self.active_client=None
    def elapsed(self): return time.monotonic()-self.started
    def remaining(self, cleanup=False): return (595 if cleanup else WORK_SECONDS)-self.elapsed()
    def command(self, argv, timeout=10, cleanup=False, **kwargs):
        remaining=self.remaining(cleanup)
        if remaining <= 0: raise BudgetExpired('Controller deadline reached')
        return subprocess.run([str(v) for v in argv],timeout=min(timeout,remaining),check=True,
                              text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs).stdout.strip()
    def client(self, argv, timeout):
        remaining=self.remaining()
        if remaining<=0: raise BudgetExpired('No client budget remains')
        process=subprocess.Popen([str(v) for v in argv],text=True,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,start_new_session=True)
        self.active_client=process
        try:
            stdout,stderr=process.communicate(timeout=min(timeout,remaining))
        except subprocess.TimeoutExpired:
            # Descendants are benchmark clients only; server is a separate
            # Docker process tree. Never crash the PostgreSQL worker as a test.
            os.killpg(process.pid,signal.SIGTERM)
            try: process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.communicate(timeout=1)
            self.active_client=None
            raise
        self.active_client=None
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode,argv,stdout,stderr)
        return stdout+'\n'+stderr
    def sleep(self, seconds):
        if self.remaining() < seconds: raise BudgetExpired('No time for readiness wait')
        time.sleep(seconds)


def trace_check(path, client_directory):
    # Full validator supplied by the native instrumentation module. Fail closed.
    from instrument_native import validate_trace
    traces=sorted(path.glob('native-trace-*.jsonl'))
    if len(traces)!=1:
        return {'complete':False,'errors':[f'Expected one normally-flushed worker trace, found {len(traces)}']}
    result=validate_trace(traces[0], client_directory)
    # Compress only AFTER native normal shutdown, never while it is measured.
    for trace in traces:
        with trace.open('rb') as source, gzip.open(str(trace)+'.gz','wb') as target:
            import shutil
            shutil.copyfileobj(source,target)
        trace.unlink()
    return result


def run_unit(budget, output, image, unit, server_cpus):
    label=unit['label']; mode='baseline' if unit['kind']=='gate' else 'echoo'
    directory=output/label; metadata=output/(label+'.stack'); metadata.mkdir()
    name='echoo-tail-'+uuid.uuid4().hex[:12]
    write_json(metadata/'lifecycle.json',{'container':name,'unit':unit,'elapsed_start':budget.elapsed()})
    launched=False; errors=[]; before=None; after=None; summary={}; trace={'complete':False,'errors':['not collected']}
    try:
        # The chosen name is ours even if Docker admission returns uncertain.
        launched=True
        budget.command(['docker','run','-d','--name',name,'--cpus=2','--memory=4g','--memory-swap=4g',
                        '--cpuset-cpus='+','.join(map(str,server_cpus)),'--pids-limit=512','--shm-size=256m',
                        '--stop-timeout=-1','--mount','type=volume,dst=/state',
                        '-e','ECHOO_DIAGNOSTIC_ARM='+unit['arm'],
                        '-p','127.0.0.1::5432','-p','127.0.0.1::5671',image,mode],timeout=10)
        details=json.loads(budget.command(['docker','inspect',name]))[0]
        compare_ci.verify_budget(details,server_cpus)
        write_json(metadata/'container-before.json',details)
        readiness_deadline=time.monotonic()+20
        while time.monotonic()<readiness_deadline:
            try:
                before=json.loads(budget.command(['docker','exec',name,'cat','/state/ready.json'],timeout=2))
                break
            except subprocess.CalledProcessError:
                if not json.loads(budget.command(['docker','inspect',name]))[0]['State']['Running']:
                    raise RuntimeError('Stack exited before readiness')
                budget.sleep(.25)
        if before is None: raise RuntimeError('20-second stack readiness bound reached')
        compare_ci.verify_settings(before,mode)
        if before['queue_depth']: raise RuntimeError('Fresh queue was not empty')
        if before.get('pg_stat_timing')!={'track_io_timing':'on','track_wal_io_timing':'on'}:
            raise RuntimeError('Synthetic PG I/O timing settings were not enabled identically')
        write_json(metadata/'before.json',before)
        for source,target in [('/opt/build-packages.txt','build-packages.txt'),('/opt/source-manifest.json','source-manifest.json'),
                              ('/opt/native-binaries.sha256','native-binaries.sha256'),('/opt/instr_time.h','instr_time.h'),
                              ('/state/pgdata/postgresql.conf','postgresql.conf')]:
            budget.command(['docker','cp',name+':'+source,metadata/target],timeout=3)
        arm_hashes = {}
        for line in (metadata/'native-binaries.sha256').read_text().splitlines():
            digest, location = line.split()
            arm_hashes[Path(location).parent.name] = digest
        if before['native_binary_sha256'] != arm_hashes.get(unit['arm']):
            raise RuntimeError('Selected binary does not match immutable image arm hash')
        if mode == 'echoo' and before['worker_library']['sha256'] != arm_hashes.get(unit['arm']):
            raise RuntimeError('Actually mapped worker binary does not match declared arm')

        ports=details['NetworkSettings']['Ports']
        pgport=ports['5432/tcp'][0]['HostPort']; mqport=ports['5671/tcp'][0]['HostPort']
        with tempfile.TemporaryDirectory(prefix='echoo-tail-certs-') as private:
            private=Path(private)
            for filename in ('ca.pem','client.pem','client.key'):
                budget.command(['docker','cp',name+':/state/certs/'+filename,private/filename],timeout=3)
                (private/filename).chmod(0o600)
            duration=GATE_SECONDS if unit['kind']=='gate' else CELL_SECONDS
            count=0 if unit['kind']=='gate' else 1
            args=[sys.executable,HERE/'client.py','--label',label,'--output',output,
                  '--dsn',f'host=127.0.0.1 port={pgport} user=bench_admin dbname=postgres connect_timeout=3',
                  '--url',f'amqps://localhost:{mqport}','--container',name,
                  '--ca',private/'ca.pem','--cert',private/'client.pem','--key',private/'client.key',
                  '--producers',count,'--consumers',count,'--offered-rate',200,'--credit',32,'--payload-bytes',1024,
                  '--db-interval',.005,'--warmup-seconds',WARMUP_SECONDS,'--duration-seconds',duration,
                  '--drain-seconds',5,'--timeout',3]
            try:
                log=budget.client(args,timeout=WARMUP_SECONDS+duration+14)
                (metadata/'client.log').write_text(log+'\n')
            except subprocess.CalledProcessError as error:
                (metadata/'client.log').write_text((error.stdout or '')+'\n'+(error.stderr or ''))
                errors.append('Client failed; see retained log and status files')
            except subprocess.TimeoutExpired as error:
                # Only terminate benchmark client group, never test server.
                errors.append('Client exceeded its bounded normal execution window')
                raise
        after=json.loads(budget.command(['docker','exec',name,'python3','/opt/echoo/bench/diagnostic/server.py',mode,'--snapshot'],timeout=5))
        write_json(metadata/'after.json',after)
        compare_ci.verify_settings(after,mode)
        if after['queue_depth']: errors.append('Final persisted queue was not empty')
        if after['native_binary_sha256'] != before['native_binary_sha256']: errors.append('Native binary changed during unit')
        write_json(metadata/'storage-delta.json',{
            'postgres_wal_lsn_bytes':compare_ci.wal_bytes(after['wal_lsn'])-compare_ci.wal_bytes(before['wal_lsn']),
            'postgres_database_bytes':after['database_bytes']-before['database_bytes'],
            'note':'Whole stage setup/warmup/measurement/drain; not physical SSD writes'})
    except BaseException as error:
        errors.append(type(error).__name__+': '+str(error))
        (metadata/'exception.txt').write_text(traceback.format_exc())
    finally:
        if launched:
            try:
                # No forced server stop: -1 permits only graceful shutdown. CLI
                # timeout bounds controller and reports an unfinished cleanup.
                budget.command(['docker','stop','--time=-1',name],timeout=14,cleanup=True)
                final=json.loads(budget.command(['docker','inspect',name],cleanup=True))[0]
                write_json(metadata/'container-after.json',final)
                if final['State']['Running'] or final['State']['OOMKilled'] or final['State']['ExitCode']:
                    errors.append('Server did not finish its normal lifecycle')
                trace_dir=metadata/'trace'; trace_dir.mkdir()
                budget.command(['docker','cp',name+':/state/trace/.',trace_dir],timeout=5,cleanup=True)
                for source,target in [('/state/postgres.log','postgres.log')]:
                    budget.command(['docker','cp',name+':'+source,metadata/target],timeout=3,cleanup=True)
                (metadata/'server.log').write_text(budget.command(['docker','logs',name],timeout=3,cleanup=True)+'\n')
                if unit['kind']=='cell':
                    trace=trace_check(trace_dir,directory)
                    # Metadata PID proves the trace belongs to the mapped binary.
                    if before is None or trace.get('worker_pid') != before.get('worker_library',{}).get('pid'):
                        trace['complete'] = False
                        trace.setdefault('errors',[]).append('Trace PID does not match verified worker library mapping')
                    write_json(metadata/'trace-validation.json',trace)
                    trace = {k:v for k,v in trace.items() if k not in ('message_stages','stage_elapsed_ns')}
                    trace['full_validation_file'] = metadata.name+'/trace-validation.json'
                    if not trace['complete']: errors.append('Native trace/mapping incomplete')
                budget.command(['docker','rm','-v',name],timeout=3,cleanup=True)
            except BaseException as error:
                errors.append('Cleanup/evidence incomplete: '+str(error))
                write_json(metadata/'cleanup-incomplete.json',{'container':name,'error':str(error),
                    'instruction':'No forced kill attempted. Inspect and finish normal shutdown; do not repeat the diagnostic.'})
        path=directory/'summary.json'
        if path.exists(): summary=json.loads(path.read_text())
        else:
            directory.mkdir(exist_ok=True)
            summary={'status':'failed','errors':['Missing client summary'],'measurement_window_complete':False}
        errors=summary.get('errors',[])+errors
        summary.update(status='failed' if errors else summary['status'],errors=errors,trace_complete=trace['complete'],
                       trace=trace,elapsed_end=budget.elapsed(),evidence_kind='ordinary_tail_diagnostic')
        if unit['kind']=='gate':
            paths=list(directory.glob('db-*.jsonl.gz')); rows=[]
            for path in paths:
                try: rows.extend(raw_rows(path))
                except Exception as error: errors.append('Partial DB evidence: '+str(error))
            if not summary.get('measurement_window_complete'): errors.append('Gate measurement incomplete')
            if not summary.get('measured_resource_sample_count'): errors.append('Gate resource sampler missing')
            summary['gate']=gate_result(rows,summary.get('measurement_start_ns',0),errors)
        else:
            summary['fixed_load_eligible']=(not errors and summary.get('offered_load_achieved_fraction',0)>=.95
                and summary.get('completed_per_s',0)>=190 and summary.get('synthetic_db_transactions_per_s',0)>=190)
            if not summary['fixed_load_eligible']: summary['comparison_scope']='diagnostic only; under-achieved load or invalid evidence'
        write_json(directory/'summary.json',summary)
        write_json(metadata/'status.json',{'errors':errors,'normal_cleanup_required':bool(errors),'elapsed_end':budget.elapsed()})
    return summary


def finalize(output,state,budget,extra):
    report={**state.report(),**extra,'controller_elapsed_seconds':budget.elapsed(),
            'hard_budget_seconds':CONTROLLER_SECONDS,'work_cutoff_seconds':WORK_SECONDS,
            'interpretation':'Ordinary mechanism evidence only; no product performance, capacity, ERP or failure-reliability qualification'}
    write_json(output/'terminal-status.json',report)
    write_json(output/'MANIFEST.json',{str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob('*')) if p.is_file() and p.name!='MANIFEST.json'})


def emergency_record(output, state, budget):
    """Small bounded fail-stop evidence; never hash/parse potentially large files."""
    (output/'MANIFEST.json').unlink(missing_ok=True)
    write_json(output/'terminal-status.json',{
        'terminal_status':'budget_insufficient','reason':'Hard deadline fail-stop',
        'controller_elapsed_seconds':budget.elapsed(),'hard_budget_seconds':CONTROLLER_SECONDS,
        'recorded_unit_labels':[r['label'] for r in state.results],
        'unrun_units':state.plan['units'][len(state.results):],
        'cleanup_may_be_incomplete':True,'active_unit_file':'active-unit.json',
        'manifest_complete':False,'automatic_retries':0,
        'instruction':'Preserve partial evidence. Finish only normal cleanup of recorded container; never rerun diagnostic.'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True); parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Output must be new; never overwrite or retry evidence')
    args.output.mkdir(parents=True)
    budget=Budget(); plan=schedule(); state=State(plan); extra={}
    write_json(args.output/'plan.json',plan)
    original_affinity=sorted(os.sched_getaffinity(0))
    def deadline(*_):
        # This independent fail-stop cannot be swallowed by broad stage cleanup
        # handlers. Fire at 599s to retain a final second for a small terminal
        # record; no large trace parsing/hashing is attempted here.
        if budget.active_client is not None:
            try: os.killpg(budget.active_client.pid,signal.SIGTERM)
            except ProcessLookupError: pass
        try:
            emergency_record(args.output,state,budget)
        finally:
            os._exit(2)
    signal.signal(signal.SIGALRM,deadline); signal.setitimer(signal.ITIMER_REAL,CONTROLLER_SECONDS-1)
    try:
        if len(original_affinity)<4: raise RuntimeError('Need four distinct logical CPU IDs; do not weaken declared budget')
        server_cpus=original_affinity[:2]; client_cpus=original_affinity[2:4]
        image=budget.command(['docker','image','inspect',args.image,'--format={{.Id}}'])
        if not image.startswith('sha256:'): raise RuntimeError('Image did not resolve to immutable ID')
        environment={'image_id':image,'platform':platform.platform(),'host_cpu_ids':original_affinity,
            'server_cpu_ids':server_cpus,'client_cpu_ids':client_cpus,'server_cpu_quota':2,
            'server_memory_bytes':4*1024**3,'server_swap_bytes':0,'clients_outside_budget':True,
            'python':platform.python_version(),'runner_image_version':os.environ.get('ImageVersion'),
            'commit':budget.command(['git','rev-parse','HEAD'],cwd=REPO),
            'dirty_worktree':budget.command(['git','status','--porcelain'],cwd=REPO),
            'docker_info':json.loads(budget.command(['docker','info','--format={{json .}}'])),
            'clock_comparison':'Server stages only CLOCK_MONOTONIC elapsed; correlate by IDs. Never subtract server and Python timestamps.',
            'storage':'Fresh volumes on same GitHub VM/session; no cache clearing, physical SSD or host isolation claim'}
        write_json(args.output/'environment.json',environment)
        (args.output/'python-inputs.txt').write_text(budget.command([sys.executable,'-m','pip','freeze'])+'\n')
        os.sched_setaffinity(0,client_cpus)
        for unit in plan['units']:
            admission=admit_unit(unit,budget.elapsed())
            if not admission['admitted']:
                extra['budget_admission']={'unit':unit,**admission}
                state.stop('budget_insufficient','Not enough time for next complete stage and cleanup reserve')
                break
            write_json(args.output/'active-unit.json',{'unit':unit,'admission':admission,'elapsed':budget.elapsed()})
            result=run_unit(budget,args.output,image,unit,server_cpus)
            state.add(unit,result)
            finalize(args.output,state,budget,extra)
            print(unit['label']+': '+state.status,flush=True)
            if state.status!='running': break
    except BaseException as error:
        extra['exception']=traceback.format_exc()
        state.stop('budget_insufficient' if isinstance(error,(BudgetExpired,subprocess.TimeoutExpired)) else 'infrastructure_insufficient',str(error))
    finally:
        os.sched_setaffinity(0,original_affinity)
        finalize(args.output,state,budget,extra)
        signal.setitimer(signal.ITIMER_REAL,0)
    # A fail-fast scientific negative is a recorded outcome, never retried.
    return 0 if state.status in ('completed','completed_diagnostic_only','environment_insufficient') else 1

if __name__=='__main__': raise SystemExit(main())
