#!/usr/bin/env python3
"""Run integration tests in an owned, disposable local cluster (never production)."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from make_test_certs import generate

REPO = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def quote(value):
    return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-config", default="pg_config")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep cluster files after shutdown; includes ephemeral private keys")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.error("initdb must run as an unprivileged OS user")
    bindir = Path(subprocess.check_output([args.pg_config, "--bindir"], text=True).strip())
    def binary(name):
        return bindir / (name + (".exe" if os.name == "nt" else ""))
    root = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="echoo-ci-"))
    root.mkdir(parents=True, exist_ok=True)
    data, certs, log = root / "data", root / "certs", root / "postgres.log"
    if data.exists() or certs.exists():
        parser.error(f"refusing to reuse existing data/certs in {root}")
    generate(certs)
    pgport, amqpport = port(), port()
    while amqpport == pgport:
        amqpport = port()
    run([binary("initdb"), "-D", data, "-U", "echoo_admin", "-A", "trust", "--encoding=UTF8", "--no-locale"])
    # Trust is confined to a newly-created loopback cluster. It is not a production recipe.
    with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write(f"\nlisten_addresses='127.0.0.1'\nport={pgport}\n")
        if os.name != "nt":
            stream.write("unix_socket_directories=''\n")
        stream.write("fsync=on\nfull_page_writes=on\nsynchronous_commit=on\n")
        stream.write("log_min_messages=info\nlog_line_prefix='%m [%p] %b '\n")
    base_env = dict(os.environ)
    base_env["PATH"] = str(bindir) + os.pathsep + base_env.get("PATH", "")
    started = False
    try:
        run([binary("pg_ctl"), "-D", data, "-l", log, "-w", "start"], env=base_env)
        started = True
        import psycopg
        dsn = f"host=127.0.0.1 port={pgport} dbname=postgres user=echoo_admin"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE DATABASE echoo_test")
        dsn = dsn.replace("dbname=postgres", "dbname=echoo_test")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION echoo_pgmq VERSION '0.1.0'")
            conn.execute("CREATE ROLE echoo_pgmq_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
            conn.execute("CREATE ROLE echoo_test_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
            conn.execute("CREATE ROLE echoo_denied LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
            conn.execute("GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_pgmq_worker")
            conn.execute("""GRANT EXECUTE ON FUNCTION echoo_pgmq.publish(text,bytea,text),
                echoo_pgmq.claim(text,text,uuid,integer),
                echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
                echoo_pgmq.authorize(text,text,text) TO echoo_pgmq_worker""")
        with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            settings = {
                "shared_preload_libraries": "echoo_pgmq", "echoo_pgmq.enabled": "on",
                "echoo_pgmq.database": "echoo_test", "echoo_pgmq.role": "echoo_pgmq_worker",
                "echoo_pgmq.listen_address": "127.0.0.1", "echoo_pgmq.port": amqpport,
                "echoo_pgmq.tls_certificate": certs / "server.pem",
                "echoo_pgmq.tls_private_key": certs / "server.key",
                "echoo_pgmq.tls_ca_file": certs / "ca.pem",
                "echoo_pgmq.visibility_seconds": 2, "echoo_pgmq.poll_interval_ms": 20,
                "echoo_pgmq.max_message_bytes": 65536, "echoo_pgmq.max_connections": 8,
                "echoo_pgmq.max_links_per_connection": 4, "echoo_pgmq.max_inflight_per_link": 4,
            }
            for key, value in settings.items():
                stream.write(f"{key}={quote(value)}\n")
        run([binary("pg_ctl"), "-D", data, "-m", "fast", "-w", "restart", "-l", log], env=base_env)
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", amqpport), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("native AMQP listener did not become ready; inspect postgres.log")
                time.sleep(0.1)
        manifest = {"pg_config": str(Path(args.pg_config).resolve()) if Path(args.pg_config).exists() else args.pg_config,
                    "postgres_version": subprocess.check_output([binary("postgres"), "--version"], text=True).strip(),
                    "python": sys.version, "platform": sys.platform, "process_crash_only": True}
        (root / "environment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        env = dict(base_env, ECHOO_TEST_DSN=dsn,
                   ECHOO_TEST_AMQP_URL=f"amqps://localhost:{amqpport}",
                   ECHOO_TEST_CERT_DIR=str(certs), ECHOO_TEST_PGDATA=str(data),
                   ECHOO_TEST_PGCTL=str(binary("pg_ctl")), ECHOO_TEST_LOG=str(log))
        pytest_args = args.pytest_args
        if pytest_args[:1] == ["--"]:
            pytest_args = pytest_args[1:]
        run([sys.executable, "-m", "pytest", *(pytest_args or ["tests"]),
             f"--junitxml={root / 'junit.xml'}"], cwd=REPO, env=env)
        # Independently execute the SQL agent's contract assertions if provided.
        for sql in sorted((REPO / "tests" / "sql").glob("*.sql")):
            run([binary("psql"), dsn, "-X", "-v", "ON_ERROR_STOP=1", "-f", sql], env=base_env)
    finally:
        if started:
            subprocess.run([str(binary("pg_ctl")), "-D", str(data), "-m", "immediate", "-w", "stop"], env=base_env)
        if log.exists():
            print("\n--- PostgreSQL log ---\n" + log.read_text(encoding="utf-8", errors="replace"), flush=True)
        print(f"Test evidence: {root}", flush=True)
        if not args.keep and args.work_dir is None:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
