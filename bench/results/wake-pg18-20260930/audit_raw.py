#!/usr/bin/env python3
"""Independent, read-only evidence audit. JSON report is written only to stdout."""
import collections, gzip, hashlib, json, math, statistics, sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
def read(p): return json.loads(p.read_text())
def load_rows(p):
    with gzip.open(p,'rt') as f: return [json.loads(line) for line in f]
def pct(xs,p):
    a=sorted(xs); return a[int((len(a)-1)*p)] if a else None
def dist(xs):
    return {'count':len(xs),'p50_ms':pct(xs,.5),'p95_ms':pct(xs,.95),'p99_ms':pct(xs,.99),'mean_ms':statistics.mean(xs) if xs else None}
def verify(got, expected, context):
    if isinstance(got,(int,float)) and isinstance(expected,(int,float)):
        assert math.isclose(got,expected,rel_tol=1e-10,abs_tol=1e-9),(context,got,expected)
    else: assert got==expected,(context,got,expected)
def lsn(s):
    a,b=s.split('/');return (int(a,16)<<32)+int(b,16)
def io(d):
    out=collections.Counter()
    for line in d['cgroup_v2']['io.stat'].splitlines():
        for item in line.split()[1:]:
            k,v=item.split('=');out[k]+=int(v)
    return out
manifest=read(root/'MANIFEST.json')
files={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.name!='MANIFEST.json'}
assert files==set(manifest),('manifest coverage mismatch',files-set(manifest),set(manifest)-files)
for filename,expected in manifest.items():
    p=(root/filename).resolve();assert p.is_relative_to(root) and p.is_file(),filename
    verify(hashlib.sha256(p.read_bytes()).hexdigest(),expected,filename)
env=read(root/'environment.json');matrix=read(root/'matrix-status.json')
assert env['commit']=='472e62698137d0e1035d04775d443f1cbcc1ae33'
assert not env['dirty_worktree'];assert env['server_cpu_ids']==[0,1] and env['client_cpu_ids']==[2,3]
assert env['clients_outside_budget'] is True
cells=[];baseline={}
for directory in sorted(root.glob('r*-baseline')):
    d=read(directory/'summary.json');baseline[d['label']]=d
for p in sorted(root.glob('*/summary.json')):
    d=read(p);label=d['label'];mode=d['broker'];stack=root/(label+'.stack')
    before=read(stack/'before.json');after=read(stack/'after.json');initial=read(stack/'container-inspect.json');final=read(stack/'container-final.json')
    verify(initial['Image'],env['image_id'],label+' image');verify(final['Image'],env['image_id'],label+' final image')
    for key,value in {'NanoCpus':2000000000,'Memory':4294967296,'MemorySwap':4294967296,'CpusetCpus':'0,1','PidsLimit':512}.items():
        verify(initial['HostConfig'][key],value,label+' '+key);verify(final['HostConfig'][key],value,label+' final '+key)
    assert final['State']['ExitCode']==0 and not final['State']['OOMKilled'],label
    for snap in (before,after):
        for key,value in {'cpu.max':'200000 100000','memory.max':'4294967296','memory.swap.max':'0','cpuset.cpus.effective':'0-1'}.items():verify(snap['cgroup_v2'][key],value,label+' '+key)
        for key,value in {'fsync':'on','full_page_writes':'on','synchronous_commit':'on','autovacuum':'on','server_version':'18.6','wal_level':'replica','shared_buffers':'65536','max_connections':'40','max_wal_size':'1024','checkpoint_timeout':'300'}.items():verify(snap['postgresql'][key],value,label+' '+key)
        if mode=='echoo':assert snap['message_table_logged'] is True
        if mode=='rabbitmq':
            verify(snap['rabbitmq_version'],'4.0.5',label+' rabbit version')
            q=next(x for x in snap['rabbitmq_queues'] if x['name']=='bench');assert q['durable'] is True and q['type']=='classic'
        assert snap['queue_depth']==0,label+' queue depth'
    events=dict(line.split() for line in after['cgroup_v2']['memory.events'].splitlines())
    assert int(events['oom'])==int(events['oom_kill'])==0,label
    assert d['client_host']['affinity']==[2,3]
    assert read(stack/'status.json')['error'] is None,label
    for status in p.parent.glob('*.status.json'):assert read(status)['error'] is None,str(status)
    conditions=d['conditions'];producers=conditions['producers'];consumers=conditions['consumers']
    rows={k:[] for k in ['producer','consumer','db']}
    for kind in rows:
        paths=sorted(p.parent.glob(kind+'-*.jsonl.gz'))
        assert len(paths)=={'producer':producers,'consumer':consumers,'db':1}[kind],(label,kind,len(paths))
        for path in paths:rows[kind].extend(load_rows(path))
    assert conditions['payload_bytes']==1024 and conditions['credit_per_consumer']==32 and conditions['publisher_inflight_per_connection']==1
    assert conditions['db_interval_s']==.005
    t0=d['measurement_start_ns'];dur=d['duration_seconds'];t1=t0+round(dur*1e9)
    inside=lambda n:t0<=n<t1
    sent_ids={x['id'] for x in rows['producer']};accepted_ids={x['id'] for x in rows['producer'] if x['accepted']};valid=[x for x in rows['consumer'] if x['valid']];received_ids={x['id'] for x in valid}
    assert len(sent_ids)==len(rows['producer']),label+' duplicate sent IDs'
    for r in rows['producer']:
        assert r['id'].startswith(d['run_id']+'/')
        if r['accepted']:verify(r['latency_ms'],(r['complete_ns']-r['start_ns'])/1e6,label+' raw publish latency')
    for r in valid:
        assert r['id'].startswith(d['run_id']+'/');verify(r['latency_ms'],(r['received_ns']-r['sent_ns'])/1e6,label+' raw E2E latency')
    for r in rows['db']:verify(r['latency_ms'],(r['complete_ns']-r['start_ns'])/1e6,label+' raw DB latency')
    counts={'duplicates':len(valid)-len(received_ids),'missing_confirmed':len(accepted_ids-received_ids),'unexpected_received':len(received_ids-sent_ids),'publish_failures':sum(not r['accepted'] for r in rows['producer']),'checksum_or_run_failures':sum(not r['valid'] for r in rows['consumer'])}
    for k,v in counts.items():verify(d[k],v,label+' '+k)
    verify(d['all_phases'],{'attempted':len(sent_ids),'accepted':len(accepted_ids),'received_unique':len(received_ids)},label+' all phases')
    account=read(p.parent/'accounting.json');verify(account,{'missing_confirmed_ids':sorted(accepted_ids-received_ids),'unexpected_ids':sorted(received_ids-sent_ids),'received_without_confirm_ids':sorted(received_ids-accepted_ids)},label+' accounting')
    cohorts={'publish':[r for r in rows['producer'] if r['accepted'] and inside(r['start_ns'])],'end_to_end':[r for r in valid if inside(r['sent_ns'])],'synthetic_db':[r for r in rows['db'] if inside(r['start_ns'])]}
    for kind,rr in cohorts.items():
        actual=dist([r['latency_ms'] for r in rr])
        for k,v in actual.items():verify(d[kind][k],v,label+' '+kind+' '+k)
    for kind,rr in [('publish',cohorts['publish']),('db',cohorts['synthetic_db'])]:
        actual=dist([r['schedule_lateness_ms'] for r in rr])
        for k,v in actual.items():verify(d[kind+'_schedule_lateness'][k],v,label+' schedule '+kind+' '+k)
    verify(d['confirmed_publish_per_s'],sum(r['accepted'] and inside(r['complete_ns']) for r in rows['producer'])/dur,label+' accepted rate')
    verify(d['completed_per_s'],len({r['id'] for r in valid if inside(r['received_ns'])})/dur,label+' receive rate')
    attempted=sum(inside(r['start_ns']) for r in rows['producer'])
    verify(d['attempted_publish_per_s'],attempted/dur,label+' attempted rate')
    verify(d['offered_load_achieved_fraction'],attempted/(dur*d['requested_rate']) if producers else None,label+' offered load')
    verify(d['synthetic_db_transactions_per_s'],len(cohorts['synthetic_db'])/dur,label+' DB rate')
    raw_events=sorted([(r['start_ns'],'attempt',r['id']) for r in rows['producer']]+[(r['complete_ns'],'accepted',r['id']) for r in rows['producer'] if r['accepted']]+[(r['received_ns'],'received',r['id']) for r in valid])
    sets={'attempt':set(),'accepted':set(),'received':set()};pos=0;backlog=read(p.parent/'backlog.json')
    for row in backlog:
        while pos<len(raw_events) and raw_events[pos][0]<=row['at_ns']:
            _,kind,msgid=raw_events[pos];sets[kind].add(msgid);pos+=1
        verify(row['attempted_not_received'],len(sets['attempt']-sets['received']),label+' backlog attempted')
        verify(row['confirmed_not_received'],len(sets['accepted']-sets['received']),label+' backlog confirmed')
    verify(d['backlog_max_confirmed_not_received'],max(r['confirmed_not_received'] for r in backlog),label+' peak confirmed backlog')
    verify(d['backlog_confirmed_not_received_at_measurement_end'],backlog[-1]['confirmed_not_received'],label+' end confirmed backlog')
    resources=load_rows(p.parent/'resources.jsonl.gz');measured=[r for r in resources if inside(r['at_ns'])]
    verify(d['resource_sample_count'],len(resources),label+' resource count');verify(d['measured_resource_sample_count'],len(measured),label+' measured resource count')
    assert len(measured)>1 and all(r['server_container']['memory_stats']['limit']==4294967296 for r in resources)
    first,last=measured[0],measured[-1];dt=(last['at_ns']-first['at_ns'])/1e9
    start_cpu=first['server_container']['cpu_stats'];end_cpu=last['server_container']['cpu_stats']
    metrics={'server_mean_cpu_cores':(end_cpu['cpu_usage']['total_usage']-start_cpu['cpu_usage']['total_usage'])/(dt*1e9),'server_peak_cgroup_mib':max(r['server_container']['memory_stats']['usage'] for r in measured)/2**20,'client_mean_cpu_cores':(last['client_cpu_seconds_sum']-first['client_cpu_seconds_sum'])/dt,'client_peak_rss_sum_mib':max(r['client_rss_bytes_sum'] for r in measured)/2**20,'measured_resource_span_seconds':dt,'measured_resource_count':len(measured),'peak_pids':max(r['server_container']['pids_stats']['current'] for r in measured),'cpu_throttled_periods_delta':end_cpu['throttling_data']['throttled_periods']-start_cpu['throttling_data']['throttled_periods'],'cpu_throttled_time_ns_delta':end_cpu['throttling_data']['throttled_time']-start_cpu['throttling_data']['throttled_time']}
    for direction in ['read','write']:
        def n(row):return sum(x['value'] for x in row['server_container']['blkio_stats']['io_service_bytes_recursive'] if x['op']==direction)
        metrics['measured_sampled_block_'+direction+'_bytes']=n(last)-n(first)
    storage=read(stack/'storage-delta.json')
    verify(storage['postgres_wal_lsn_bytes'],lsn(after['wal_lsn'])-lsn(before['wal_lsn']),label+' WAL delta')
    verify(storage['postgres_database_bytes'],after['database_bytes']-before['database_bytes'],label+' DB growth')
    before_io,after_io=io(before),io(after)
    metrics.update(whole_cell_postgres_wal_lsn_bytes=storage['postgres_wal_lsn_bytes'],whole_cell_postgres_database_bytes=storage['postgres_database_bytes'],whole_cell_cgroup_io={k:after_io[k]-before_io[k] for k in after_io})
    if mode!='baseline' and env['run_kind']=='measurement':
        base=baseline[d['baseline_label']]['synthetic_db']['p95_ms'];p95=d['synthetic_db']['p95_ms'];increase=100*(p95/base-1)
        verify(d['baseline_db_p95_ms'],base,label+' baseline p95');verify(d['db_p95_increase_percent'],increase,label+' DB percent');verify(d['provisional_db_p95_le_10_percent'],increase<=10,label+' provisional target')
        metrics['db_p95_absolute_increase_ms']=p95-base
    if env['run_kind']=='measurement':assert d['warmup_seconds']==30 and dur==120
    assert d['measurement_window_complete'] and d['observed_measurement_seconds']==dur
    d['independent_audit_metrics']=metrics;cells.append(d)
verify(len(cells),matrix['recorded_cells'],'matrix count');verify(len(cells),env['expected_cells'],'expected cells')
assert matrix['status']=='completed' and matrix['remaining_cells_not_run']==0
if env['run_kind']=='measurement':
    expected={'r%d-baseline'%r for r in range(1,4)}|{f'r{r}-{broker}-{p}x{p}-{rate}pps' for r in range(1,4) for broker in ['echoo','rabbitmq'] for p in [1,4] for rate in [200,400]}
    verify({r['label'] for r in cells},expected,'complete matrix labels')
groups=[]
for key in sorted({(d['broker'],d['profile'],d['requested_rate']) for d in cells}):
    rr=[d for d in cells if (d['broker'],d['profile'],d['requested_rate'])==key]
    result={'broker':key[0],'profile':key[1],'requested_rate':key[2],'repetitions':len(rr),'labels':[d['label'] for d in rr],'statistics_note':'median and range of per-cell metrics, not pooled percentile estimates','metrics':{}}
    for metric in ['confirmed_publish_per_s','completed_per_s','attempted_publish_per_s','offered_load_achieved_fraction','synthetic_db_transactions_per_s','db_p95_increase_percent']:
        vv=[d[metric] for d in rr if d.get(metric) is not None]
        if vv:result['metrics'][metric]={'median':statistics.median(vv),'min':min(vv),'max':max(vv)}
    for category in ['publish','end_to_end','synthetic_db','publish_schedule_lateness','db_schedule_lateness']:
        for metric in ['p50_ms','p95_ms','p99_ms']:
            vv=[d[category][metric] for d in rr if d[category][metric] is not None]
            if vv:result['metrics'][category+'_'+metric]={'median':statistics.median(vv),'min':min(vv),'max':max(vv)}
    for metric in rr[0]['independent_audit_metrics']:
        vv=[d['independent_audit_metrics'][metric] for d in rr if isinstance(d['independent_audit_metrics'][metric],(int,float))]
        if vv:result['metrics'][metric]={'median':statistics.median(vv),'min':min(vv),'max':max(vv)}
    groups.append(result)
json.dump({'audit_version':1,'source_commit':env['commit'],'manifest_file_count':len(manifest),'manifest_sha256':hashlib.sha256((root/'MANIFEST.json').read_bytes()).hexdigest(),'environment':env,'matrix_status':matrix,'totals':{'cells':len(cells),'failed_cells':sum(d['status']!='passed' for d in cells),'all_phases':{k:sum(d['all_phases'][k] for d in cells) for k in ['attempted','accepted','received_unique']},'measured_resource_samples':sum(d['measured_resource_sample_count'] for d in cells),'provisional_target_false':sum(d.get('provisional_db_p95_le_10_percent') is False for d in cells),'under_95_percent_offered_load':sum(d.get('offered_load_achieved_fraction') is not None and d['offered_load_achieved_fraction']<.95 for d in cells),**{k:sum(d[k] for d in cells) for k in ['duplicates','missing_confirmed','unexpected_received','publish_failures','checksum_or_run_failures']}},'groups':groups,'cells':cells},sys.stdout,indent=2,ensure_ascii=False)
print()
