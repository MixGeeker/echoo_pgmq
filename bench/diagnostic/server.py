#!/usr/bin/env python3
"""Disposable CI stack supervisor. Only graceful start/stop, no fault injection."""
import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import hashlib
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from make_test_certs import generate

ROOT = Path(os.environ.get("ECHOO_DIAGNOSTIC_STATE", "/state"))
PG = Path(os.environ.get("ECHOO_DIAGNOSTIC_PG", "/opt/pg18/bin"))


def command(args, **kwargs):
    kwargs.setdefault("timeout", 15)
    return subprocess.check_output([str(v) for v in args], text=True, **kwargs).strip()


def sql(statement):
    return command([PG / "psql", "-h", "127.0.0.1", "-p", "5432", "-U", "bench_admin",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-At", "-c", statement])


def wait_port(port):
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), .5):
                return
        except OSError:
            time.sleep(.25)
    raise RuntimeError(f"service on {port} did not start")


def rabbit_env():
    return dict(os.environ, RABBITMQ_MNESIA_BASE=str(ROOT / "rabbit/data"),
                RABBITMQ_LOG_BASE=str(ROOT / "rabbit/log"),
                RABBITMQ_CONFIG_FILE=str(ROOT / "rabbit/rabbitmq"),
                RABBITMQ_ENABLED_PLUGINS_FILE=str(ROOT / "rabbit/enabled_plugins"),
                RABBITMQ_PLUGINS_EXPAND_DIR=str(ROOT / "rabbit/plugins-expand"),
                RABBITMQ_NODENAME="echoobench@localhost", RABBITMQ_DIST_PORT="25672",
                ERL_CRASH_DUMP=str(ROOT / "rabbit/erl_crash.dump"),
                RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="+S 2:2", RABBITMQ_CTL_ERL_ARGS="+S 1:1")


def snapshot(mode):
    result = {"mode": mode,
              "postgresql": json.loads(sql("SELECT json_object_agg(name,setting) FROM pg_settings WHERE name IN "
                    "('fsync','full_page_writes','synchronous_commit','wal_level','shared_buffers',"
                    "'max_connections','max_wal_size','checkpoint_timeout','autovacuum','server_version')")),
              "wal_lsn": sql("SELECT pg_current_wal_lsn()"),
              "database_bytes": int(sql("SELECT pg_database_size(current_database())"))}
    if mode == "echoo":
        result["queue_depth"] = int(sql("SELECT message_count FROM echoo_pgmq.queues WHERE name='bench'"))
        result["message_table_logged"] = sql("SELECT relpersistence FROM pg_class WHERE oid='echoo_pgmq.messages'::regclass") == "p"
    elif mode == "rabbitmq":
        rows = command(["rabbitmqctl", "--quiet", "list_queues", "name", "messages", "messages_ready",
                        "messages_unacknowledged", "durable", "type", "--formatter=json"], env=rabbit_env())
        result["rabbitmq_queues"] = json.loads(rows)
        result["queue_depth"] = next(int(row["messages"]) for row in result["rabbitmq_queues"] if row["name"] == "bench")
        result["rabbitmq_version"] = command(["rabbitmq-diagnostics", "-q", "server_version"], env=rabbit_env())
        result["erlang_version"] = command(["erl", "+S", "1:1", "-noshell", "-eval",
                  'io:format("~s", [erlang:system_info(otp_release)]), halt().'])
    else:
        result["queue_depth"] = 0
    result["cgroup_v2"] = {}
    for filename in ("cpu.max", "cpuset.cpus.effective", "memory.max", "memory.swap.max", "memory.events", "io.stat"):
        path = Path("/sys/fs/cgroup") / filename
        if path.is_file():
            result["cgroup_v2"][filename] = path.read_text().strip()
    result["arm"] = os.environ.get("ECHOO_DIAGNOSTIC_ARM", "A")
    binary = ROOT / "selected/echoo_pgmq.so"
    result["native_binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    result["worker_library"] = None
    if mode == "echoo":
        pids = json.loads(sql("SELECT coalesce(json_agg(pid),'[]'::json) FROM pg_stat_activity WHERE backend_type='echoo_pgmq AMQP listener'"))
        if len(pids) != 1:
            raise RuntimeError("Expected exactly one owned native diagnostic worker")
        pid = pids[0]
        stat = binary.stat()
        expected_device = (os.major(stat.st_dev), os.minor(stat.st_dev))
        mappings = []
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and "echoo_pgmq.so" in fields[5]:
                device = tuple(int(x,16) for x in fields[3].split(":"))
                if int(fields[4]) != stat.st_ino or device != expected_device or Path(fields[5]).resolve() != binary.resolve():
                    raise RuntimeError("Worker mapped a different native binary than selected arm")
                mappings.append(line)
        if not mappings:
            raise RuntimeError("Owned worker native library mapping is unavailable")
        result["worker_library"] = {"pid": pid, "selected_realpath": str(binary.resolve()),
            "inode": stat.st_ino, "device": expected_device, "mappings": mappings,
            "sha256": result["native_binary_sha256"]}

    result["clock_notes"] = {"diagnostic": "CLOCK_MONOTONIC", "postgres_instr_time": "Inspect build instr_time.h; not subtracted from Python clock"}
    for name, query in {
        "io": "SELECT coalesce(json_agg(t),'[]'::json) FROM pg_stat_io t",
        "checkpointer": "SELECT coalesce(json_agg(t),'[]'::json) FROM pg_stat_checkpointer t",
        "timing": "SELECT json_object_agg(name,setting) FROM pg_settings WHERE name IN ('track_io_timing','track_wal_io_timing')",
    }.items():
        try:
            result["pg_stat_"+name] = json.loads(sql(query))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            result["pg_stat_"+name] = {"unavailable": str(error)}
    for name in ("/proc/self/timens_offsets", "/proc/self/ns/time"):
        try:
            result[name] = os.readlink(name) if name.endswith("/time") else Path(name).read_text()
        except OSError as error:
            result[name] = {"unavailable": str(error)}
    return result


def serve(mode):
    root = ROOT
    selected = ROOT / "selected"
    selected.mkdir()
    arm = os.environ.get("ECHOO_DIAGNOSTIC_ARM", "A")
    if arm not in ("A", "B"):
        raise ValueError("unknown diagnostic arm")
    shutil.copyfile(Path(os.environ.get("ECHOO_DIAGNOSTIC_ARMS", "/opt/arms")) / arm / "echoo_pgmq.so", selected / "echoo_pgmq.so")
    trace = ROOT / "trace"
    trace.mkdir()
    os.environ["ECHOO_DIAGNOSTIC_TRACE_DIR"] = str(trace)
    data, certs = root / "pgdata", root / "certs"
    generate(certs)
    command([PG / "initdb", "-D", data, "-U", "bench_admin", "-A", "trust", "--no-locale", "--encoding=UTF8"])
    # Ports are published only on host loopback by compare_ci.py. No credentials
    # or real business data are used. PostgreSQL trust auth is TEST ONLY.
    with (data / "postgresql.conf").open("a") as conf:
        conf.write("\nlisten_addresses='*'\nunix_socket_directories=''\nport=5432\n"
                   "fsync=on\nfull_page_writes=on\nsynchronous_commit=on\nwal_level=replica\n"
                   "shared_buffers='512MB'\nmax_connections=40\nmax_wal_size='1GB'\n"
                   "checkpoint_timeout='5min'\nautovacuum=on\ntrack_io_timing=on\ntrack_wal_io_timing=on\n")
    with (data / "pg_hba.conf").open("a") as hba:
        hba.write("\nhost all all 0.0.0.0/0 trust\n")
    started = False
    rabbit = epmd = None
    stop = False
    def request_stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    def pg(action):
        command([PG / "pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "-m", "fast", action])
    try:
        pg("start")
        started = True
        sql("CREATE EXTENSION echoo_pgmq VERSION '0.1.1'; CREATE ROLE echoo_test_user NOLOGIN; "
            "CREATE ROLE echoo_benchmark_worker NOLOGIN; "
            "GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_benchmark_worker; "
            "GRANT EXECUTE ON FUNCTION echoo_pgmq.publish(text,bytea,text), "
            "echoo_pgmq.claim(text,text,uuid,integer), echoo_pgmq.settle(text,bigint,bigint,uuid,text,text), "
            "echoo_pgmq.authorize(text,text,text) TO echoo_benchmark_worker; "
            "SELECT echoo_pgmq.create_queue('bench',30000,67108864); "
            "SELECT echoo_pgmq.grant_queue('bench','echoo_test_user')")
        with (data / "postgresql.conf").open("a") as conf:
            conf.write("shared_preload_libraries='echoo_pgmq'\n")
            settings = {"enabled": "on" if mode == "echoo" else "off",
                        "database": "postgres", "role": "echoo_benchmark_worker",
                        "listen_address": "0.0.0.0", "port": 5671,
                        "tls_certificate": certs / "server.pem", "tls_private_key": certs / "server.key",
                        "tls_ca_file": certs / "ca.pem", "poll_interval_ms": 2,
                        "visibility_seconds": 60, "max_inflight_per_link": 32}
            for key, value in settings.items():
                conf.write(f"echoo_pgmq.{key}='{value}'\n")
        pg("restart")
        if mode == "rabbitmq":
            rmq = root / "rabbit"
            rmq.mkdir()
            definitions = {"users": [{"name": "echoo_test_user", "password_hash": "", "tags": []}],
                           "vhosts": [{"name": "/"}], "permissions": [{"user": "echoo_test_user", "vhost": "/",
                           "configure": "^bench$", "write": "^(bench|amq.default)$", "read": "^bench$"}],
                           "queues": [{"name": "bench", "vhost": "/", "durable": True, "auto_delete": False,
                                       "arguments": {"x-queue-type": "classic"}}]}
            (rmq / "definitions.json").write_text(json.dumps(definitions))
            (rmq / "enabled_plugins").write_text("[rabbitmq_auth_mechanism_ssl].\n")
            (rmq / "rabbitmq.conf").write_text(f"""listeners.tcp = none
listeners.ssl.1 = 0.0.0.0:5671
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
            # Explicit foreground EPMD avoids relying on Rabbit's image entrypoint
            # or Erlang auto-daemon startup. It is charged to the same stack cgroup.
            epmd_port = os.environ.get("ERL_EPMD_PORT", "4369")
            epmd = subprocess.Popen(["epmd", "-address", "127.0.0.1", "-port", epmd_port], env=rabbit_env())
            wait_port(int(epmd_port))
            with (rmq / "server.log").open("w") as log:
                rabbit = subprocess.Popen(["rabbitmq-server"], env=rabbit_env(), stdout=log, stderr=subprocess.STDOUT)
        if mode != "baseline":
            wait_port(5671)
        for attempt in range(60):
            try:
                result = snapshot(mode)
                break
            except (subprocess.CalledProcessError, StopIteration):
                if attempt == 59:
                    raise
                time.sleep(.5)
        temporary = root / "ready.json.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(root / "ready.json")
        while not stop:
            if rabbit and rabbit.poll() is not None:
                raise RuntimeError("RabbitMQ exited unexpectedly")
            time.sleep(.25)
    finally:
        try:
            if rabbit and rabbit.poll() is None:
                command(["rabbitmqctl", "stop"], env=rabbit_env())
                rabbit.wait(30)
        finally:
            try:
                if started:
                    pg("stop")
            finally:
                if epmd and epmd.poll() is None:
                    epmd.terminate()
                    epmd.wait(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "echoo"))
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(snapshot(args.mode)))
    else:
        serve(args.mode)
