"""Bounded synthetic latency cohorts; invoked only in a disposable CI cluster."""
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def distribution(values):
    ordered = sorted(values)
    return {'count': len(ordered), 'p50_ms': ordered[int((len(ordered)-1)*.5)] if ordered else None,
            'p95_ms': ordered[int((len(ordered)-1)*.95)] if ordered else None,
            'p99_ms': ordered[int((len(ordered)-1)*.99)] if ordered else None,
            'mean_ms': statistics.mean(ordered) if ordered else None}


def summarize(row):
    samples = row['samples']
    expected = 0 if row['kind'] == 'idle' else 256 if row['kind'] == 'backlog' else row['window'] * 8
    if row['status'] != 'passed' or len(samples) != expected or row['complete_count'] != expected:
        raise ValueError('incomplete cohort')
    if len({s['id'] for s in samples}) != expected:
        raise ValueError('duplicate cohort IDs')
    for s in samples:
        if not s['send_ms'] <= s['receive_ms'] <= s['settled_ms'] or s['accepted_ms'] < s['send_ms']:
            raise ValueError('missing or invalid phase timestamp')
    result = {key: row[key] for key in ('kind','window','status','complete_count')}
    for name, end in [('publish_accept','accepted_ms'),('send_receive','receive_ms'),('send_settled','settled_ms')]:
        result[name] = distribution([s[end]-s['send_ms'] for s in samples])
    result['receive_settled'] = distribution([s['settled_ms']-s['receive_ms'] for s in samples])
    result['elapsed_seconds'] = (row['end_ms']-row.get('consume_start_ms',row['start_ms']))/1000
    result['completed_per_second'] = expected / result['elapsed_seconds']
    # Backlog latency includes intentional preload; drain throughput is separate.
    result['latency_includes_preload'] = row['kind'] == 'backlog'
    return result


def snapshot(db):
    db.execute('SELECT pg_stat_clear_snapshot()')
    return {
        'wal': db.execute('SELECT row_to_json(w) FROM pg_stat_wal w').fetchone()[0],
        'database': db.execute("SELECT row_to_json(d) FROM pg_stat_database d WHERE datname=current_database()").fetchone()[0],
        'lock_waiters': db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'").fetchone()[0],
    }


def main():
    import psutil
    import psycopg
    output = Path(os.environ['ECHOO_WAKEUP_OUTPUT']); output.mkdir(parents=True,exist_ok=True)
    report = {'schema':1,'platform':platform.platform(),'logical_cpus':os.cpu_count(),
              'memory_bytes':psutil.virtual_memory().total,'scope':'synthetic mechanism benchmark, not store capacity or Windows 11 certification',
              'statistics_note':'boundary PostgreSQL snapshots may lag backend reporting; no polling sampler during traffic',
              'results':[],'status':'running'}
    def save(): (output/'summary.json').write_text(json.dumps(report,indent=2,default=str))
    save()
    try:
        with psycopg.connect(os.environ['ECHOO_TEST_DSN'],autocommit=True) as db:
            settings = dict(db.execute("SELECT name,setting FROM pg_settings WHERE name IN ('fsync','full_page_writes','synchronous_commit','echoo_pgmq.poll_interval_ms','echoo_pgmq.max_inflight_per_link','track_io_timing','track_wal_io_timing')").fetchall())
            report['settings']=settings
            assert all(settings[x]=='on' for x in ('fsync','full_page_writes','synchronous_commit'))
            pid=db.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]
            worker=psutil.Process(pid)
            for kind,window in [('idle',1),*[(k,w) for w in (1,8,32) for k in ('sparse','backlog')]]:
                queue=f'wakeup_{kind}_{window}'
                db.execute('SELECT echoo_pgmq.create_queue(%s)',(queue,))
                db.execute("SELECT echoo_pgmq.grant_queue(%s,'echoo_test_user')",(queue,))
                before=snapshot(db); cpu=worker.cpu_times(); start=time.perf_counter()
                path=output/f'{kind}-{window}.json'
                proc=subprocess.run(['node',str(ROOT/'bench/wakeup_client.js'),queue,kind,str(window),str(path)],capture_output=True,text=True,timeout=90)
                elapsed=time.perf_counter()-start; after_cpu=worker.cpu_times()
                (output/f'{kind}-{window}.stdout.txt').write_text(proc.stdout)
                (output/f'{kind}-{window}.stderr.txt').write_text(proc.stderr)
                details={'kind':kind,'window':window,'client_exit':proc.returncode,'wall_seconds':elapsed,
                         'worker_cpu_seconds':after_cpu.user+after_cpu.system-cpu.user-cpu.system,
                         'before':before,'after':snapshot(db)}
                details['worker_one_core_cpu_pct']=100*details['worker_cpu_seconds']/elapsed
                report['results'].append(details);save()
                proc.check_returncode()
                details['cohort']=summarize(json.loads(path.read_text()))
                retained=db.execute('SELECT message_count FROM echoo_pgmq.queues WHERE name=%s',(queue,)).fetchone()[0]
                assert retained==0, f'unsettled retained messages: {retained}'
                assert db.execute("SELECT pid FROM pg_stat_activity WHERE usename='echoo_pgmq_worker'").fetchone()[0]==pid
                details['remaining_messages']=retained;save()
            report['status']='passed';save()
    except BaseException as error:
        report['status']='failed';report['error']=repr(error);save();raise


if __name__ == '__main__': main()
