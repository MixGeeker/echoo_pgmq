#!/usr/bin/env python3
"""Run a disposable Linux PostgreSQL/RabbitMQ AMQP1.0 comparison.

Requires installed extension and RabbitMQ/Erlang binaries. No production endpoint
is accepted. All database rows, identities and certificates are synthetic.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import psycopg

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from make_test_certs import generate


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(command, **kwargs):
    return subprocess.run([str(x) for x in command], check=True, **kwargs)


def ready(port):
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", port), .5):
                return
        except OSError:
            time.sleep(.25)
    raise RuntimeError(f"local test service on port {port} did not start")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pg-config", default="pg_config")
    p.add_argument("--rabbitmq-server", default="rabbitmq-server")
    p.add_argument("--rabbitmq-version", required=True)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, default=REPO / "bench" / "results")
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--messages", type=int, default=5000)
    p.add_argument("--baseline-seconds", type=float, default=10)
    a = p.parse_args()
    if os.name == "nt":
        p.error("This orchestration is Linux-only; run.py supports externally prepared native Windows endpoints")
    if a.work_dir.exists():
        p.error("Use a new empty work-dir; refusing to overwrite data")
    root = a.work_dir.resolve()
    root.mkdir(parents=True, mode=0o700)
    certs = root / "certs"
    generate(certs)
    pgroot = Path(subprocess.check_output([a.pg_config, "--bindir"], text=True).strip())
    pgport, mqport, rmqport, epmdport, distport = [free_port() for _ in range(5)]
    data, pglog = root / "pgdata", root / "postgres.log"
    env = dict(os.environ)
    env["PATH"] = str(pgroot) + os.pathsep + env.get("PATH", "")
    run([pgroot / "initdb", "-D", data, "-U", "bench_admin", "-A", "trust", "--no-locale", "--encoding=UTF8"])
    config = data / "postgresql.conf"
    with config.open("a") as f:
        f.write(f"\nlisten_addresses='127.0.0.1'\nunix_socket_directories=''\nport={pgport}\n")
        f.write("fsync=on\nfull_page_writes=on\nsynchronous_commit=on\nshared_buffers='128MB'\nmax_connections=30\n")
    dsn = f"host=127.0.0.1 port={pgport} user=bench_admin dbname=postgres"
    pg_started = False
    rabbit = epmd = None
    def pg(action):
        run([pgroot / "pg_ctl", "-D", data, "-l", pglog, "-w", "-m", "fast", action], env=env)
    try:
        pg("start")
        pg_started = True
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'")
            db.execute("CREATE ROLE echoo_test_user NOLOGIN")
            db.execute("CREATE ROLE echoo_benchmark_worker NOLOGIN")
            db.execute("GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_benchmark_worker")
            db.execute("""GRANT EXECUTE ON FUNCTION echoo_pgmq.publish(text,bytea,text),
                       echoo_pgmq.claim(text,text,uuid,integer), echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
                       echoo_pgmq.authorize(text,text,text) TO echoo_benchmark_worker""")
            db.execute("SELECT echoo_pgmq.create_queue('bench', 30000, 67108864)")
            db.execute("SELECT echoo_pgmq.grant_queue('bench','echoo_test_user')")
            settings = dict(db.execute("SELECT name,setting FROM pg_settings WHERE name IN ('fsync','synchronous_commit','full_page_writes','shared_buffers','wal_sync_method','server_version')").fetchall())
        with config.open("a") as f:
            f.write("shared_preload_libraries='echoo_pgmq'\nechoo_pgmq.enabled=off\n")
            for key, value in {"database": "postgres", "role": "echoo_benchmark_worker",
                               "listen_address": "127.0.0.1", "port": mqport,
                               "tls_certificate": certs / "server.pem", "tls_private_key": certs / "server.key",
                               "tls_ca_file": certs / "ca.pem", "poll_interval_ms": 2,
                               "visibility_seconds": 60, "max_inflight_per_link": 32}.items():
                f.write(f"echoo_pgmq.{key}='{str(value).replace(chr(39), chr(39)*2)}'\n")
        rmq = root / "rabbit"
        rmq.mkdir()
        definitions = {"users": [{"name": "echoo_test_user", "password_hash": "", "tags": []}],
                       "vhosts": [{"name": "/"}],
                       "permissions": [{"user": "echoo_test_user", "vhost": "/", "configure": "^bench$", "write": "^(bench|amq.default)$", "read": "^bench$"}],
                       "queues": [{"name": "bench", "vhost": "/", "durable": True, "auto_delete": False,
                                   "arguments": {"x-queue-type": "classic"}}]}
        (rmq / "definitions.json").write_text(json.dumps(definitions))
        (rmq / "enabled_plugins").write_text("[rabbitmq_auth_mechanism_ssl].\n")
        (rmq / "rabbitmq.conf").write_text(f"""listeners.tcp = none
listeners.ssl.1 = 127.0.0.1:{rmqport}
ssl_options.cacertfile = {certs / 'ca.pem'}
ssl_options.certfile = {certs / 'server.pem'}
ssl_options.keyfile = {certs / 'server.key'}
ssl_options.verify = verify_peer
ssl_options.fail_if_no_peer_cert = true
auth_mechanisms.1 = EXTERNAL
ssl_cert_login_from = common_name
definitions.import_backend = local_filesystem
definitions.local.path = {rmq / 'definitions.json'}
vm_memory_high_watermark.absolute = 512MiB
disk_free_limit.absolute = 500MB
log.console = true
log.console.level = warning
""")
        rmqenv = dict(env, RABBITMQ_MNESIA_BASE=str(rmq / "data"), RABBITMQ_LOG_BASE=str(rmq / "log"),
                      RABBITMQ_CONFIG_FILE=str(rmq / "rabbitmq"), RABBITMQ_ENABLED_PLUGINS_FILE=str(rmq / "enabled_plugins"),
                      RABBITMQ_PLUGINS_EXPAND_DIR=str(rmq / "plugins-expand"), RABBITMQ_PID_FILE=str(rmq / "rabbit.pid"),
                      RABBITMQ_NODENAME="echoocompare@localhost", RABBITMQ_DIST_PORT=str(distport),
                      ERL_EPMD_PORT=str(epmdport), RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="+S 2:2",
                      HOME=str(rmq))
        a.output.mkdir(parents=True, exist_ok=True)
        (a.output / "environment.json").write_text(json.dumps({"postgresql": settings,
           "rabbitmq": a.rabbitmq_version, "rabbitmq_erlang_schedulers": 2, "proton_python": "0.40.0",
           "echoo_poll_interval_ms": 2, "note": "Cloud Linux capacity probe; not Win11, physical power loss or real ERP qualification",
           "ordering": "Each repetition baseline, then alternating broker order; both receive same synthetic DB load"}, indent=2)+"\n")
        def execute(label, version, queue_type, url=None, address="bench", virtual_host="localhost", pids=None):
            cmd = [sys.executable, REPO / "bench" / "run.py", "--label", label, "--broker-version", version,
                   "--queue-type", queue_type, "--dsn", dsn, "--messages", a.messages,
                   "--output", a.output, "--server-pid", *(pids or [])]
            if url:
                cmd += ["--url", url, "--address", address, "--virtual-host", virtual_host,
                        "--ca", certs / "ca.pem", "--cert", certs / "client.pem", "--key", certs / "client.key"]
            else:
                cmd += ["--baseline-seconds", a.baseline_seconds]
            run(cmd, env=env)
        for repetition in range(a.repetitions):
            with config.open("a") as f:
                f.write("echoo_pgmq.enabled=off\n")
            pg("restart")
            pgpid = int((data / "postmaster.pid").read_text().splitlines()[0])
            execute(f"baseline-{repetition+1}", settings["server_version"], "none", pids=[pgpid])
            order = ["echoo", "rabbitmq"] if repetition % 2 == 0 else ["rabbitmq", "echoo"]
            for broker in order:
                with config.open("a") as f:
                    f.write(f"echoo_pgmq.enabled={'on' if broker == 'echoo' else 'off'}\n")
                pg("restart")
                pgpid = int((data / "postmaster.pid").read_text().splitlines()[0])
                if broker == "echoo":
                    ready(mqport)
                    execute(f"echoo-{repetition+1}", "0.1.1-dev", "logged-postgresql", f"amqps://localhost:{mqport}", pids=[pgpid])
                    with psycopg.connect(dsn, autocommit=True) as db:
                        # Settlements use at-least-once semantics; wait for receiver ACKs to commit.
                        for _ in range(100):
                            if db.execute("SELECT count(*) FROM echoo_pgmq.messages").fetchone()[0] == 0:
                                break
                            time.sleep(.05)
                        else:
                            raise RuntimeError("echoo retained unexpected messages after benchmark")
                else:
                    epmd = subprocess.Popen(["epmd", "-address", "127.0.0.1", "-port", str(epmdport)], env=rmqenv)
                    with (rmq / "server.log").open("a") as log:
                        rabbit = subprocess.Popen([a.rabbitmq_server], env=rmqenv, stdout=log, stderr=subprocess.STDOUT)
                        ready(rmqport)
                        execute(f"rabbitmq-{repetition+1}", a.rabbitmq_version, "classic-durable",
                                f"amqps://localhost:{rmqport}", address="/queues/bench", virtual_host="vhost:/", pids=[pgpid, rabbit.pid])
                        rabbit.terminate()
                        rabbit.wait(30)
                        rabbit = None
                    epmd.terminate()
                    epmd.wait(10)
                    epmd = None
    finally:
        for process in (rabbit, epmd):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(20)
                except subprocess.TimeoutExpired:
                    process.kill()
        if pg_started:
            subprocess.run([str(pgroot / "pg_ctl"), "-D", str(data), "-w", "-m", "fast", "stop"], env=env)
        print(f"Disposable state (contains test keys): {root}")


if __name__ == "__main__":
    main()
