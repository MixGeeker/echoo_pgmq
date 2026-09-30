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

# Explicit allowlist: adding a new test does not silently expand automatic CI
# into adversarial-input, resource-exhaustion, fault-injection or crash testing.
ORDINARY_TESTS = [
    "tests/sql/test_bounded_storage.py::test_concurrent_same_key_creates_one_message",
    "tests/sql/test_bounded_storage.py::test_concurrent_key_quota_cannot_overbook",
    "tests/sql/test_bounded_storage.py::test_rollback_releases_message_bytes_and_key_budget",
    "tests/sql/test_bounded_storage.py::test_global_limits_apply_across_queues_and_rollback",
    "tests/sql/test_bounded_storage.py::test_logical_dump_restores_payload_acl_ledger_receipts_and_sequences",
    "tests/sql/test_bounded_storage.py::test_binary_helper_preserves_application_bytes",
    "tests/sql/test_bounded_storage.py::test_expired_key_reuses_bounded_slot",
    "tests/sql/test_bounded_storage.py::test_dropped_or_recreated_role_does_not_inherit_queue_acl",
    "tests/sql/test_bounded_storage.py::test_administrative_retry_invalidates_old_receipt",
    "tests/sql/test_bounded_storage.py::test_expired_final_attempt_cleanup_is_bounded",
    "tests/sql/test_bounded_storage.py::test_poison_quarantine_does_not_deadlock_with_late_ack",
    "tests/test_storage_integration.py::test_concurrent_claims_are_unique",
    "tests/test_storage_integration.py::test_concurrent_capacity_cannot_overbook",
    "tests/test_storage_integration.py::test_ordinary_role_cannot_call_private_worker_api",
    "tests/test_storage_integration.py::test_upgrade_failure_rolls_back_and_retry_preserves_data",
    "tests/test_candidate_packaging.py::test_candidate_manifest_and_archive_checksums",
    "tests/test_candidate_packaging.py::test_candidate_archive_tampering_fails",
    "tests/test_candidate_packaging.py::test_candidate_manifest_tampering_fails",
    "tests/test_candidate_packaging.py::test_candidate_unsafe_members_fail",
    "tests/test_candidate_packaging.py::test_candidate_unlisted_member_fails",
    "tests/test_harness.py::test_sql_script_uses_explicit_options_and_dsn",
    "tests/test_harness.py::test_sql_script_rejects_ignored_argument_warning",
    "tests/test_harness.py::test_sql_script_requires_completion_marker",
    "tests/test_harness.py::test_sql_script_propagates_nonzero_exit",
    "tests/test_amqp_integration.py::test_binary_and_metadata_roundtrip_preserves_encoded_wire",
    "tests/test_amqp_integration.py::test_sender_accepted_is_not_sent_before_commit",
    "tests/test_amqp_integration.py::test_abandoned_delivery_is_redelivered",
    "tests/test_amqp_integration.py::test_late_ack_does_not_delete_new_delivery",
    "tests/test_amqp_integration.py::test_release_retries_and_reject_retains_dead_letter",
    "tests/test_spi_plan_cache.py::test_repeated_worker_operations_and_empty_claims",
    "tests/test_spi_plan_cache.py::test_capacity_rejection_then_recovery_uses_same_worker",
    "tests/test_spi_plan_cache.py::test_function_cost_change_revalidates_worker_plans",
    "tests/test_spi_plan_cache.py::test_lock_timeout_then_recovery_reuses_settings_plan",
]


INDEPENDENT_CLIENT_TESTS = [
    "tests/test_independent_client.py::test_rhea_binary_metadata_roundtrip",
    "tests/test_independent_client.py::test_rhea_release_redelivers",
    "tests/test_independent_client.py::test_rhea_graceful_reconnect_redelivers",
]


def run(command, **kwargs):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def run_sql_script(psql, dsn, sql, env, required_marker=None):
    """Run the actual SQL file; successful process exit alone is insufficient."""
    # Some Windows psql versions stop parsing at a positional DSN. All
    # arguments here are explicit options, with no positional connection value.
    command = [str(psql), "-X", "-v", "ON_ERROR_STOP=1", "--dbname", dsn, "--file", str(sql)]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    print(result.stdout, flush=True)
    result.check_returncode()
    if "extra command-line argument" in result.stdout.lower():
        raise RuntimeError("psql ignored command-line arguments; SQL test configuration is invalid")
    if required_marker and required_marker not in result.stdout:
        raise RuntimeError(f"{Path(sql).name} did not emit its completion marker")
    return result.stdout


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
    parser.add_argument("--ordinary", action="store_true",
                        help="run the explicit ordinary regression allowlist, without security/fault/crash tests")
    parser.add_argument("--independent-client", action="store_true",
                        help="add reviewed rhea interoperability cases to --ordinary; requires locked npm dependencies")
    parser.add_argument("--keep", action="store_true", help="keep cluster files after shutdown; includes ephemeral private keys")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.independent_client and not args.ordinary:
        parser.error("--independent-client requires --ordinary")
    if args.ordinary and args.pytest_args:
        parser.error("--ordinary uses a fixed reviewed allowlist; pytest overrides are not accepted")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.error("initdb must run as an unprivileged OS user")
    independent_client = None
    if args.independent_client:
        node = shutil.which("node")
        if not node:
            parser.error("--independent-client requires Node.js and npm ci --prefix tests/interop --ignore-scripts")
        independent_client = json.loads(subprocess.check_output(
            [node, str(REPO / "tests" / "interop" / "verify_dependencies.js")], text=True))
    ordinary_tests = ORDINARY_TESTS + (INDEPENDENT_CLIENT_TESTS if args.independent_client else [])
    version = subprocess.check_output([args.pg_config, "--version"], text=True).strip()
    major = int(version.split()[1].split(".")[0])
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
    # PG16 requires LOGIN even for background workers. Deny its external access
    # before generic temporary-cluster trust rules. PG17 supports NOLOGIN bypass.
    hba = data / "pg_hba.conf"
    reject = "host all echoo_pgmq_worker 0.0.0.0/0 reject\nhost all echoo_pgmq_worker ::0/0 reject\n"
    if os.name != "nt":
        reject = "local all echoo_pgmq_worker reject\n" + reject
    hba.write_text(reject + hba.read_text(encoding="utf-8"), encoding="utf-8")
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
            conn.execute(f"CREATE ROLE echoo_pgmq_worker {'NOLOGIN' if major >= 17 else 'LOGIN'} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
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
                    "python": sys.version, "platform": sys.platform,
                    "test_scope": "ordinary-regression" if args.ordinary else "qualification-or-explicit-selection",
                    "ordinary_test_allowlist": ordinary_tests if args.ordinary else None,
                    "independent_client": independent_client,
                    "process_crash_tests_enabled": not args.ordinary,
                    "physical_power_loss_tested": False,
                    "qualification_status": "blocked_security_review",
                    "sql_scripts_passed": []}
        (root / "environment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        env = dict(base_env, ECHOO_TEST_DSN=dsn,
                   ECHOO_TEST_AMQP_URL=f"amqps://localhost:{amqpport}",
                   ECHOO_TEST_CERT_DIR=str(certs), ECHOO_TEST_PGDATA=str(data),
                   ECHOO_TEST_PGCTL=str(binary("pg_ctl")), ECHOO_TEST_LOG=str(log))
        if args.independent_client:
            env["ECHOO_TEST_INTEROP_REPORT"] = str(root / "independent-client.jsonl")
        pytest_args = ordinary_tests if args.ordinary else args.pytest_args
        if pytest_args[:1] == ["--"]:
            pytest_args = pytest_args[1:]
        run([sys.executable, "-m", "pytest", *(pytest_args or ["tests"]),
             f"--junitxml={root / 'junit.xml'}"], cwd=REPO, env=env)
        # Independently execute the SQL agent's contract assertions if provided.
        sql_tests = [REPO / "tests" / "sql" / "core.sql"] if args.ordinary else sorted((REPO / "tests" / "sql").glob("*.sql"))
        for sql in sql_tests:
            run_sql_script(binary("psql"), dsn, sql, base_env,
                           required_marker="core SQL tests passed" if sql.name == "core.sql" else None)
            manifest["sql_scripts_passed"].append(sql.name)
            (root / "environment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    finally:
        if started:
            subprocess.run([str(binary("pg_ctl")), "-D", str(data), "-m", "fast" if args.ordinary else "immediate", "-w", "stop"], env=base_env)
        if log.exists():
            print("\n--- PostgreSQL log ---\n" + log.read_text(encoding="utf-8", errors="replace"), flush=True)
        print(f"Test evidence: {root}", flush=True)
        if not args.keep and args.work_dir is None:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
