"""Linux/PG18/Proton-OpenSSL normal checks; no latency pass threshold.

Creates only a new disposable cluster. Not Windows/SChannel or PG16/17 coverage.
"""
import os,sys,time,json,pathlib,socket,subprocess,uuid,argparse
import psycopg
from proton import SSLDomain,Message,Delivery,Timeout
from proton.utils import BlockingConnection
from contextlib import closing
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('root',type=pathlib.Path,help='New disposable work directory; existing paths are refused')
parser.add_argument('--pg-config',required=True)
parser.add_argument('--repo',type=pathlib.Path,required=True)
a=parser.parse_args();ROOT=a.root.resolve()
if ROOT.exists():parser.error('refusing to reuse or modify an existing work directory')
if sys.platform!='linux':parser.error('this standalone harness covers Linux/Proton-OpenSSL only')
if not subprocess.check_output([a.pg_config,'--version'],text=True).strip().startswith('PostgreSQL 18.'):
 parser.error('this standalone harness covers PostgreSQL18 only')
if not (a.repo/'scripts/make_test_certs.py').is_file():parser.error('--repo must be the Echoo source checkout')
PG=pathlib.Path(subprocess.check_output([a.pg_config,'--bindir'],text=True).strip());data=ROOT/'data';cert=ROOT/'certs';conf=data/'postgresql.conf'
if not ROOT.exists():
 ROOT.mkdir(parents=True,mode=0o700)
 sys.path.insert(0,str(a.repo/'scripts'))
 from make_test_certs import generate
 generate(cert)
 def free_port():
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
 pgport,mqport=free_port(),free_port()
 with (ROOT/'setup.log').open('w') as log:
  subprocess.run([str(PG/'initdb'),'-D',str(data),'-U','bench_admin','-A','trust','--no-locale','--encoding=UTF8'],check=True,stdout=log,stderr=subprocess.STDOUT)
 with conf.open('a') as f:f.write(f"\nlisten_addresses='127.0.0.1'\nunix_socket_directories=''\nport={pgport}\nfsync=on\nfull_page_writes=on\nsynchronous_commit=on\n")
 subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-l',str(ROOT/'postgres.log'),'-w','start'],check=True,stdout=subprocess.DEVNULL)
 try:
  with psycopg.connect(f'host=127.0.0.1 port={pgport} user=bench_admin dbname=postgres',autocommit=True) as db:
   db.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'")
   db.execute('CREATE ROLE echoo_test_user NOLOGIN');db.execute('CREATE ROLE echoo_benchmark_worker NOLOGIN')
   db.execute('GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_benchmark_worker')
   db.execute('GRANT EXECUTE ON FUNCTION echoo_pgmq.publish(text,bytea,text),echoo_pgmq.claim(text,text,uuid,integer),echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),echoo_pgmq.authorize(text,text,text) TO echoo_benchmark_worker')
 finally:subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
 with conf.open('a') as f:
  f.write("shared_preload_libraries='echoo_pgmq'\n")
  for k,v in dict(database='postgres',role='echoo_benchmark_worker',listen_address='127.0.0.1',port=mqport,tls_certificate=cert/'server.pem',tls_private_key=cert/'server.key',tls_ca_file=cert/'ca.pem',poll_interval_ms=50,visibility_seconds=60).items():f.write(f"echoo_pgmq.{k}='{v}'\n")
settings={}
for line in conf.read_text().splitlines():
 if line.startswith(('port=','echoo_pgmq.port=')):
  k,v=line.split('=',1);settings[k]=v.strip("'\" ")
pgport=settings['port'];mqport=settings['echoo_pgmq.port'];dsn=f'host=127.0.0.1 port={pgport} user=bench_admin dbname=postgres'
with conf.open('a') as f:f.write('echoo_pgmq.enabled=on\n')
subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-l',str(ROOT/'postgres.log'),'-w','start'],check=True,stdout=subprocess.DEVNULL)
report=[];conns=[]
def check(condition,message):
 if not condition:raise AssertionError(message)
def connect():
 domain=SSLDomain(SSLDomain.MODE_CLIENT);domain.set_trusted_ca_db(str(cert/'ca.pem'));domain.set_credentials(str(cert/'client.pem'),str(cert/'client.key'),None);domain.set_peer_authentication(SSLDomain.VERIFY_PEER_NAME)
 conn=BlockingConnection(f'amqps://localhost:{mqport}',ssl_domain=domain,timeout=5,allowed_mechs='EXTERNAL',virtual_host='localhost',sni='localhost');conns.append(conn);return conn
def queue(db):
 q='normal_'+uuid.uuid4().hex;db.execute('select echoo_pgmq.create_queue(%s)',(q,));db.execute("select echoo_pgmq.grant_queue(%s,'echoo_test_user')",(q,));return q
def rows(db,q):return db.execute('select m.state,m.attempts from echoo_pgmq.messages m join echoo_pgmq.queues q using(queue_id) where q.name=%s',(q,)).fetchall()
def receive_bytes(receiver):
 message=receiver.receive(timeout=5)
 return bytes(message.body)  # Keep Message alive while copying Proton's borrowed view.
def drained(db,q):
 deadline=time.monotonic()+5
 while rows(db,q):
  if time.monotonic()>deadline:raise AssertionError('normal ACKs did not drain queue')
  time.sleep(.025)
try:
 for _ in range(100):
  try:
   with socket.create_connection(('127.0.0.1',int(mqport)),.1):break
  except OSError:time.sleep(.05)
 with psycopg.connect(dsn,autocommit=True) as db:
  for producer_first in [True,False]:
   q=queue(db)
   if producer_first:
    pc=connect();sender=pc.create_sender(q);rc=connect();receiver=rc.create_receiver(q,credit=1)
   else:
    rc=connect();receiver=rc.create_receiver(q,credit=1);pc=connect();sender=pc.create_sender(q)
   rc.create_sender(q).close()  # Flush receiver credit through a normal protocol roundtrip.
   mid=str(uuid.uuid4());check(sender.send(Message(body=b'normal-wake',id=mid,durable=True)).remote_state==Delivery.ACCEPTED,'publish not accepted')
   received=receiver.receive(timeout=5);check(received.id==mid and received.body==b'normal-wake','order roundtrip mismatch');receiver.accept();receiver.close();sender.close();pc.close();rc.close();drained(db,q)
   report.append({'case':'consumer_visited_before_publisher' if producer_first else 'publisher_visited_before_consumer','passed':True})
  q=queue(db);other=queue(db);pc=connect();sender=pc.create_sender(q);pulse=pc.create_sender(other);rc=connect();no_credit=rc.create_receiver(q,credit=0);other_rc=connect();other_receiver=other_rc.create_receiver(other,credit=1)
  rc.create_sender(q).close();other_rc.create_sender(other).close()
  check(sender.send(Message(body=b'waiting-for-credit',durable=True)).remote_state==Delivery.ACCEPTED,'publish not accepted')
  check(pulse.send(Message(body=b'unrelated-pulse',durable=True)).remote_state==Delivery.ACCEPTED,'pulse not accepted')
  check((pulse_body:=receive_bytes(other_receiver))==b'unrelated-pulse',f'wrong queue received pulse: {pulse_body!r}');other_receiver.accept();other_receiver.close();drained(db,other)
  check(rows(db,q)==[('ready',0)],'zero-credit matching receiver claimed a message')
  no_credit.link.flow(1);check(receive_bytes(no_credit)==b'waiting-for-credit','credit resumption mismatch');no_credit.accept();no_credit.close();sender.close();pulse.close();drained(db,q)
  report.append({'case':'matching_zero_credit_stays_ready_across_unrelated_delivery_then_resumes','passed':True})
  q=queue(db);rc=connect();receiver=rc.create_receiver(q,credit=1)
  rc.create_sender(q).close()
  db.execute("select echoo_pgmq.grant_queue(%s,'bench_admin')",(q,));db.execute('select echoo_pgmq.enqueue_binary(%s,%s)',(q,b'sql-fallback'))
  check(receive_bytes(receiver)==b'sql-fallback','SQL fallback mismatch');receiver.accept();receiver.close();drained(db,q)
  report.append({'case':'ordinary_sql_enqueue_fallback','passed':True})
  q=queue(db);receivers=[]
  for _ in range(4):
   rc=connect();receivers.append(rc.create_receiver(q,credit=1));rc.create_sender(q).close()
  sender=connect().create_sender(q);expected=set()
  for i in range(4):
   mid=str(uuid.uuid4());expected.add(mid);check(sender.send(Message(id=mid,body=bytes([i]),durable=True)).remote_state==Delivery.ACCEPTED,'4-consumer send failed')
  seen=[];deadline=time.monotonic()+5
  while len(seen)<4:
   for receiver in receivers:
    try:message=receiver.receive(timeout=.25)
    except Timeout:continue
    seen.append(str(message.id));receiver.accept()
    if len(seen)==4:break
   check(time.monotonic()<deadline or len(seen)==4,'four-consumer normal drain timeout')
  for receiver in receivers:receiver.close()
  check(len(set(seen))==4 and set(seen)==expected,'4-consumer IDs differ');sender.close();drained(db,q)
  report.append({'case':'four_credited_consumers_unique_and_drained','passed':True})
 print(json.dumps(report,indent=2));(ROOT/'normal-wake-checks.json').write_text(json.dumps(report,indent=2))
finally:
 for conn in reversed(conns):
  try:conn.close()
  except Exception:pass
 subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
