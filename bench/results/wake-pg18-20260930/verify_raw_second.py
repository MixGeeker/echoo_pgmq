#!/usr/bin/env python3
"""Independent read-only verification/rollup of preserved ordinary-load evidence.
Never imports benchmark implementation or connects to brokers. Writes JSON stdout.
"""
import argparse, collections, gzip, hashlib, json, math, statistics
from pathlib import Path

def load(p): return json.loads(p.read_text())
def rows(p):
    with gzip.open(p, 'rt') as f: return [json.loads(s) for s in f]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def same(a,b,label):
    if isinstance(a,(int,float)) and not isinstance(a,bool) and isinstance(b,(int,float)):
        assert math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-10),(label,a,b)
    else: assert a==b,(label,a,b)
def dist(values):
    v=sorted(values);n=len(v)
    return {'count':n, **{f'p{q}_ms':v[int((n-1)*q/100)] if n else None for q in [50,95,99]},'mean_ms':statistics.mean(v) if n else None}
def flatio(s):
    c=collections.Counter()
    for line in s.splitlines():
        for pair in line.split()[1:]:
            k,v=pair.split('=');c[k]+=int(v)
    return dict(c)
def blk(s,op): return sum(x['value'] for x in (s['server_container']['blkio_stats'].get('io_service_bytes_recursive') or []) if x['op'].lower()==op)
def lsn(v): h,l=v.split('/');return (int(h,16)<<32)+int(l,16)
def stats(v): return {'median':statistics.median(v),'min':min(v),'max':max(v)} if v else None

p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--expected-sha');a=p.parse_args();root=a.directory.resolve()
manifest=load(root/'MANIFEST.json');actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.name!='MANIFEST.json'}
assert actual==set(manifest),('manifest file set',actual-set(manifest),set(manifest)-actual)
for name,digest in manifest.items():
    f=(root/name).resolve();assert f.is_relative_to(root) and f.is_file(),name;same(sha(f),digest,name+' sha')
env=load(root/'environment.json');matrix=load(root/'matrix-status.json')
if a.expected_sha: same(env['commit'],a.expected_sha,'source SHA')
same(env['dirty_worktree'],'','clean source');same(env['server_total_cpu_quota'],2,'server CPU');same(env['server_total_memory_bytes'],4*1024**3,'server memory');same(env['server_swap_bytes'],0,'server swap')
assert set(env['server_cpu_ids']).isdisjoint(env['client_cpu_ids']);same(env['clients_outside_budget'],True,'client isolation')
results=[];volume_ids=[];all_errors=[]
for path in sorted(root.glob('*/summary.json')):
    d=load(path);label=d['label'];stack=root/(label+'.stack');audit={};errors=[]
    d['independent_audit_metrics']=audit;results.append(d)
    for sp in sorted(path.parent.glob('*.status.json')):
        error=load(sp).get('error')
        if error: errors.append({'file':sp.name,'error':error})
    if (stack/'status.json').exists() and load(stack/'status.json').get('error'):errors.append({'file':str(stack.name+'/status.json'),'error':load(stack/'status.json')['error']})
    d['independent_client_stack_errors']=errors;all_errors.extend({'label':label,**x} for x in errors)
    if 'measurement_start_ns' not in d: d['independent_verification']='no measurement available';continue
    start=d['measurement_start_ns'];duration=d['duration_seconds'];end=start+int(duration*1e9);epoch=start-int(d['warmup_seconds']*1e9)
    window=lambda t:start<=t<end
    pub=[x for f in sorted(path.parent.glob('producer-*.jsonl.gz')) for x in rows(f)]
    con=[x for f in sorted(path.parent.glob('consumer-*.jsonl.gz')) for x in rows(f)]
    db=[x for f in sorted(path.parent.glob('db-*.jsonl.gz')) for x in rows(f)]
    resources=rows(path.parent/'resources.jsonl.gz');measured=[s for s in resources if window(s['at_ns'])]
    sent={x['id']:x for x in pub};accepted={x['id']:x for x in pub if x['accepted']};valid=[x for x in con if x['valid']];received={x['id']:x for x in valid}
    same(len(sent),len(pub),label+' unique producer IDs')
    for x in pub:
        assert x['id'].startswith(d['run_id']+'/'),(label,'producer run ID')
        if x.get('latency_ms') is not None:same(x['latency_ms'],(x['complete_ns']-x['start_ns'])/1e6,label+' producer latency timestamp')
    for x in valid:
        assert x['id'].startswith(d['run_id']+'/'),(label,'consumer run ID')
        if x['id'] in sent:same(x['sent_ns'],sent[x['id']]['start_ns'],label+' sent timestamp')
        same(x['latency_ms'],(x['received_ns']-x['sent_ns'])/1e6,label+' consumer latency timestamp')
    for x in db:same(x['latency_ms'],(x['complete_ns']-x['start_ns'])/1e6,label+' db latency timestamp')
    pc=[x for x in pub if x['accepted'] and window(x['start_ns'])];cc=[x for x in valid if window(x['sent_ns'])];dc=[x for x in db if window(x['start_ns'])]
    for name,values in [('publish',[x['latency_ms'] for x in pc]),('end_to_end',[x['latency_ms'] for x in cc]),('synthetic_db',[x['latency_ms'] for x in dc]),('publish_schedule_lateness',[x['schedule_lateness_ms'] for x in pc]),('db_schedule_lateness',[x['schedule_lateness_ms'] for x in dc])]:
        for k,v in dist(values).items():same(d[name][k],v,label+'/'+name+'/'+k)
    accounting={'missing_confirmed_ids':sorted(set(accepted)-set(received)),'unexpected_ids':sorted(set(received)-set(sent)),'received_without_confirm_ids':sorted(set(received)-set(accepted))}
    same(load(path.parent/'accounting.json'),accounting,label+' accounting')
    expected={'confirmed_publish_per_s':sum(window(x['complete_ns']) for x in accepted.values())/duration,'completed_per_s':len({x['id'] for x in valid if window(x['received_ns'])})/duration,'attempted_publish_per_s':sum(window(x['start_ns']) for x in pub)/duration,'synthetic_db_transactions_per_s':len(dc)/duration,'duplicates':len(valid)-len(received),'missing_confirmed':len(accounting['missing_confirmed_ids']),'unexpected_received':len(accounting['unexpected_ids']),'publish_failures':sum(not x['accepted'] for x in pub),'checksum_or_run_failures':sum(not x['valid'] for x in con),'resource_sample_count':len(resources),'measured_resource_sample_count':len(measured),'all_phases':{'attempted':len(sent),'accepted':len(accepted),'received_unique':len(received)}}
    expected['offered_load_achieved_fraction']=expected['attempted_publish_per_s']/d['requested_rate'] if d['requested_rate'] else None
    for k,v in expected.items():same(d[k],v,label+'/'+k)
    # Recompute each preserved one-second snapshot directly from IDs and timestamps.
    backlog=load(path.parent/'backlog.json')
    same([x['at_ns'] for x in backlog],list(range(epoch,end+1,10**9)),label+' backlog timestamp grid')
    ev=sorted([(x['start_ns'],0,x['id']) for x in pub]+[(x['complete_ns'],1,x['id']) for x in accepted.values()]+[(x['received_ns'],2,x['id']) for x in valid]);sets=[set(),set(),set()];index=0
    for snap in backlog:
        while index<len(ev) and ev[index][0]<=snap['at_ns']:
            _,kind,identifier=ev[index];sets[kind].add(identifier);index+=1
        same(snap['elapsed_s'],(snap['at_ns']-epoch)/1e9,label+' backlog elapsed')
        same(snap['attempted_not_received'],len(sets[0]-sets[2]),label+' backlog attempted')
        same(snap['confirmed_not_received'],len(sets[1]-sets[2]),label+' backlog confirmed')
    same(d['backlog_max_confirmed_not_received'],max((x['confirmed_not_received'] for x in backlog),default=0),label+' backlog max')
    same(d['backlog_confirmed_not_received_at_measurement_end'],backlog[-1]['confirmed_not_received'] if backlog else None,label+' backlog end')
    same(d['backlog_max_client_outstanding'],max((x['client_outstanding'] for x in resources),default=0),label+' live outstanding')
    same(d['client_host']['affinity'],env['client_cpu_ids'],label+' affinity')
    if len(measured)>=2:
        first,last=measured[0],measured[-1];span=(last['at_ns']-first['at_ns'])/1e9
        cpu=lambda s:s['server_container']['cpu_stats']['cpu_usage']['total_usage']
        audit.update(server_mean_cpu_cores=(cpu(last)-cpu(first))/1e9/span,server_peak_cgroup_mib=max(s['server_container']['memory_stats']['usage'] for s in measured)/2**20,client_mean_cpu_cores=(last['client_cpu_seconds_sum']-first['client_cpu_seconds_sum'])/span,client_peak_rss_sum_mib=max(s['client_rss_bytes_sum'] for s in measured)/2**20,measured_resource_span_seconds=span,measured_resource_count=len(measured),peak_pids=max(s['server_container']['pids_stats']['current'] for s in measured),cpu_throttled_periods_delta=last['server_container']['cpu_stats']['throttling_data']['throttled_periods']-first['server_container']['cpu_stats']['throttling_data']['throttled_periods'],cpu_throttled_time_ns_delta=last['server_container']['cpu_stats']['throttling_data']['throttled_time']-first['server_container']['cpu_stats']['throttling_data']['throttled_time'],measured_sampled_block_read_bytes=blk(last,'read')-blk(first,'read'),measured_sampled_block_write_bytes=blk(last,'write')-blk(first,'write'))
    inspect=load(stack/'container-inspect.json');final=load(stack/'container-final.json');before=load(stack/'before.json');after=load(stack/'after.json')
    same(inspect['Image'],env['image_id'],label+' immutable image')
    for phase in [inspect,final]:
        for k,v in {'NanoCpus':2_000_000_000,'Memory':4*1024**3,'MemorySwap':4*1024**3,'CpusetCpus':','.join(map(str,env['server_cpu_ids'])),'PidsLimit':512}.items():same(phase['HostConfig'][k],v,label+'/HostConfig/'+k)
    volume_ids.extend(v['Name'] for v in inspect['Mounts'] if v['Destination']=='/state')
    for snapshot in [before,after]:
        for k,v in {'cpu.max':'200000 100000','memory.max':str(4*1024**3),'memory.swap.max':'0','cpuset.cpus.effective':'0-1'}.items():same(snapshot['cgroup_v2'][k],v,label+'/cgroup/'+k)
        for k,v in {'fsync':'on','full_page_writes':'on','synchronous_commit':'on','autovacuum':'on','server_version':'18.6','wal_level':'replica','shared_buffers':'65536','max_connections':'40','max_wal_size':'1024','checkpoint_timeout':'300'}.items():same(snapshot['postgresql'][k],v,label+'/pg/'+k)
        if d['broker']=='echoo':same(snapshot['message_table_logged'],True,label+' WAL table')
        if d['broker']=='rabbitmq':
            same(snapshot['rabbitmq_version'],'4.0.5',label+' Rabbit version');q=next(x for x in snapshot['rabbitmq_queues'] if x['name']=='bench');same(q['durable'],True,label+' queue durable');same(q['type'],'classic',label+' queue type')
    audit['final_queue_depth']=after['queue_depth'];audit['container_exit_code']=final['State']['ExitCode'];audit['container_oom_killed']=final['State']['OOMKilled'];audit['cgroup_memory_events']={k:int(v) for k,v in (line.split() for line in after['cgroup_v2']['memory.events'].splitlines())}
    audit['whole_cell_postgres_wal_lsn_bytes']=lsn(after['wal_lsn'])-lsn(before['wal_lsn']);audit['whole_cell_postgres_database_bytes']=after['database_bytes']-before['database_bytes']
    b,aio=flatio(before['cgroup_v2']['io.stat']),flatio(after['cgroup_v2']['io.stat']);audit['whole_cell_cgroup_io']={k:aio.get(k,0)-b.get(k,0) for k in sorted(set(b)|set(aio))}
    storage=load(stack/'storage-delta.json')
    same(storage['postgres_wal_lsn_bytes'],audit['whole_cell_postgres_wal_lsn_bytes'],label+' WAL delta');same(storage['postgres_database_bytes'],audit['whole_cell_postgres_database_bytes'],label+' data delta')
    d['independent_verification']='raw rates, timestamp latencies, recorded scheduling lateness, IDs, backlog, resources and configuration verified'
assert len(volume_ids)==len(set(volume_ids)), 'new volume per cell'
bylabel={d['label']:d for d in results}
for d in results:
    if 'baseline_label' in d and env['run_kind']=='measurement':
        base=bylabel[d['baseline_label']]['synthetic_db']['p95_ms'];same(d['baseline_db_p95_ms'],base,d['label']+' matched baseline')
        delta=d['synthetic_db']['p95_ms']-base;percent=delta/base*100
        same(d['db_p95_increase_percent'],percent,d['label']+' DB increase');same(d['provisional_db_p95_le_10_percent'],percent<=10,d['label']+' threshold');d['independent_audit_metrics']['db_p95_absolute_increase_ms']=delta
allgroups=collections.defaultdict(list)
for d in results:allgroups[(d['broker'],d.get('profile'),d.get('requested_rate'))].append(d)
groups=[]
for (broker,profile,rate),rr in sorted(allgroups.items()):
    metrics=collections.defaultdict(list)
    for d in rr:
        for k in ['confirmed_publish_per_s','completed_per_s','attempted_publish_per_s','offered_load_achieved_fraction','synthetic_db_transactions_per_s','db_p95_increase_percent']:
            if d.get(k) is not None:metrics[k].append(d[k])
        for n in ['publish','end_to_end','synthetic_db','publish_schedule_lateness','db_schedule_lateness']:
            for q in [50,95,99]:
                v=d.get(n,{}).get(f'p{q}_ms')
                if v is not None:metrics[n+f'_p{q}_ms'].append(v)
        for k,v in d['independent_audit_metrics'].items():
            if isinstance(v,(int,float)) and not isinstance(v,bool):metrics[k].append(v)
    groups.append({'broker':broker,'profile':profile,'requested_rate':rate,'repetitions':len(rr),'labels':[d['label'] for d in rr],'statistics_note':'median and range of per-cell metrics, not pooled percentile estimates','metrics':{k:stats(v) for k,v in metrics.items()}})
totals={'cells':len(results),'failed':sum(d['status']!='passed' for d in results),'provisional_db_target_false':sum(d.get('provisional_db_p95_le_10_percent') is False for d in results),'client_stack_errors':all_errors}
for k in ['duplicates','missing_confirmed','unexpected_received','publish_failures','checksum_or_run_failures','measured_resource_sample_count']:totals[k]=sum(d.get(k,0) for d in results)
for k in ['attempted','accepted','received_unique']:totals['all_'+k]=sum(d.get('all_phases',{}).get(k,0) for d in results)
totals['measured_db_transactions']=sum(d.get('synthetic_db',{}).get('count',0) for d in results)
same(matrix['recorded_cells'],len(results),'matrix recorded cells')
print(json.dumps({'audit_version':2,'source_commit':env['commit'],'manifest_file_count':len(manifest),'manifest_sha256':sha(root/'MANIFEST.json'),'environment':env,'matrix_status':matrix,'totals':totals,'groups':groups,'cells':results,'limitations':['Consumer raw rows retain validation results, not payload bodies. Offline checks verify valid flags, timestamps and identities, not body hashes a second time.','Schedule lateness is checked against raw recorded fields; due timestamps are not preserved.','Resources are sampled; storage deltas span the entire cell before shutdown.','All genuine slow/failing cells remain in this output. Infrastructure aborts leave unrun cells unmeasured.']},indent=2,ensure_ascii=False))
