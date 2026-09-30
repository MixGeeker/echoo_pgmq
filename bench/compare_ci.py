#!/usr/bin/env python3
"""Clean-runner, equal-total-budget, ordinary fixed-load AMQP comparison.

Never points at production services. One new volume/container per cell. Does not
invoke security tests, fault injection, fuzzing, broker crashes or saturation.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import psutil

REPO = Path(__file__).resolve().parents[1]
BASE_IMAGE = "rabbitmq:4.0.5@sha256:82ee1d53d63c646b2fbaacfe4c5fb0e01a447047df585d16836ae236637bb15a"
MEMORY_BYTES = 4 * 1024 ** 3


def command(args, **kwargs):
    kwargs.setdefault("timeout", 180)
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs).strip()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def wal_bytes(lsn):
    high, low = lsn.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def inspect(name):
    return json.loads(command(["docker", "inspect", name]))[0]


def verify_budget(details, server_cpus):
    conf = details["HostConfig"]
    expected = {"NanoCpus": 2_000_000_000, "Memory": MEMORY_BYTES, "MemorySwap": MEMORY_BYTES,
                "CpusetCpus": ",".join(map(str, server_cpus)), "PidsLimit": 512}
    for key, value in expected.items():
        if conf.get(key) != value:
            raise RuntimeError(f"server budget mismatch: {key}: {conf.get(key)!r} != {value!r}")


def verify_settings(snapshot, mode):
    settings = snapshot["postgresql"]
    for key in ("fsync", "full_page_writes", "synchronous_commit", "autovacuum"):
        if settings[key] != "on":
            raise RuntimeError(f"required PostgreSQL setting {key} is not on")
    if settings["server_version"] != "18.6" or settings["wal_level"] != "replica":
        raise RuntimeError("PostgreSQL version/WAL settings do not match pinned experiment")
    for key, expected in {"shared_buffers": "65536", "max_connections": "40", "max_wal_size": "1024",
                          "checkpoint_timeout": "300"}.items():
        if settings[key] != expected:
            raise RuntimeError(f"PostgreSQL {key} differs from pinned setting: {settings[key]} != {expected}")
    if mode == "echoo" and not snapshot["message_table_logged"]:
        raise RuntimeError("echoo message storage must be a normal logged table")
    if mode == "rabbitmq":
        if snapshot["rabbitmq_version"] != "4.0.5":
            raise RuntimeError("RabbitMQ version differs from pinned comparison")
        queue = next(q for q in snapshot["rabbitmq_queues"] if q["name"] == "bench")
        if queue["durable"] is not True or queue["type"] != "classic":
            raise RuntimeError("RabbitMQ queue must be durable classic")
    cgroup = snapshot["cgroup_v2"]
    quota, period = map(int, cgroup["cpu.max"].split())
    if quota / period != 2 or int(cgroup["memory.max"]) != MEMORY_BYTES or cgroup["memory.swap.max"] != "0":
        raise RuntimeError("in-container cgroup limits are not the declared shared budget")


def run_cell(a, image, label, mode, profile, rate, server_cpus):
    name = "echoo-bench-" + uuid.uuid4().hex[:12]
    metadata = a.output / f"{label}.stack"
    metadata.mkdir()
    error = None
    launched = False
    try:
        command(["docker", "run", "-d", "--name", name, "--cpus=2", "--memory=4g", "--memory-swap=4g",
                 "--cpuset-cpus=" + ",".join(map(str, server_cpus)), "--pids-limit=512", "--shm-size=256m",
                 "--stop-timeout=60", "--mount", "type=volume,dst=/state",
                 "-p", "127.0.0.1::5432", "-p", "127.0.0.1::5671", image, mode])
        launched = True
        details = inspect(name)
        verify_budget(details, server_cpus)
        write_json(metadata / "container-inspect.json", details)
        for _ in range(240):
            result = subprocess.run(["docker", "exec", name, "cat", "/state/ready.json"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                before = json.loads(result.stdout)
                break
            if not inspect(name)["State"]["Running"]:
                raise RuntimeError("benchmark stack exited before readiness")
            time.sleep(.5)
        else:
            raise RuntimeError("benchmark stack readiness timed out")
        verify_settings(before, mode)
        if before["queue_depth"]:
            raise RuntimeError("new disposable queue was not empty")
        write_json(metadata / "before.json", before)
        command(["docker", "cp", name + ":/opt/build-packages.txt", metadata / "build-packages.txt"])
        command(["docker", "cp", name + ":/state/pgdata/postgresql.conf", metadata / "postgresql.conf"])
        if mode == "rabbitmq":
            command(["docker", "cp", name + ":/state/rabbit/rabbitmq.conf", metadata / "rabbitmq.conf"])
        ports = details["NetworkSettings"]["Ports"]
        pgport = ports["5432/tcp"][0]["HostPort"]
        mqport = ports["5671/tcp"][0]["HostPort"]
        # Test PKI goes only in a temporary private directory, never artifacts.
        with tempfile.TemporaryDirectory(prefix="echoo-ci-client-") as private:
            private = Path(private)
            for filename in ("ca.pem", "client.pem", "client.key"):
                command(["docker", "cp", f"{name}:/state/certs/{filename}", private / filename])
                (private / filename).chmod(0o600)
            producers, consumers = (0, 0) if mode == "baseline" else profile
            client = [sys.executable, REPO / "bench/run_duration.py", "--label", label,
                      "--dsn", f"host=127.0.0.1 port={pgport} user=bench_admin dbname=postgres",
                      "--url", f"amqps://localhost:{mqport}", "--container", name,
                      "--address", "/queues/bench" if mode == "rabbitmq" else "bench",
                      "--virtual-host", "vhost:/" if mode == "rabbitmq" else "localhost",
                      "--ca", private / "ca.pem", "--cert", private / "client.pem", "--key", private / "client.key",
                      "--producers", producers, "--consumers", consumers, "--offered-rate", rate,
                      "--warmup-seconds", a.warmup_seconds, "--duration-seconds", a.duration_seconds,
                      "--drain-seconds", 30, "--output", a.output]
            with (metadata / "client.log").open("w") as log:
                result = subprocess.run([str(x) for x in client], stdout=log, stderr=subprocess.STDOUT,
                                        timeout=a.warmup_seconds + a.duration_seconds + 240)
            if result.returncode:
                error = f"client measurement failed (exit {result.returncode}); retained complete/partial evidence"
        # Outside the measured window. No management plugin/sampling CLI runs
        # inside the server budget while the timed clients are active.
        after = json.loads(command(["docker", "exec", name, "python3", "/opt/echoo/bench/ci_server.py", mode, "--snapshot"]))
        for _ in range(20):
            if not after["queue_depth"]:
                break
            time.sleep(.25)
            after = json.loads(command(["docker", "exec", name, "python3", "/opt/echoo/bench/ci_server.py", mode, "--snapshot"]))
        write_json(metadata / "after.json", after)
        verify_settings(after, mode)
        write_json(metadata / "storage-delta.json", {
            "postgres_wal_lsn_bytes": wal_bytes(after["wal_lsn"]) - wal_bytes(before["wal_lsn"]),
            "postgres_database_bytes": after["database_bytes"] - before["database_bytes"],
            "note": "Whole cell including warmup, measured load, setup and drain; WAL LSN bytes are not physical SSD writes. Docker I/O counters are in resource samples."})
        events = dict(line.split() for line in after["cgroup_v2"]["memory.events"].splitlines())
        if after["queue_depth"] or int(events.get("oom_kill", 0)):
            error = f"final queue depth={after['queue_depth']}; cgroup oom_kill={events.get('oom_kill')}"
    except BaseException:
        error = traceback.format_exc()
    finally:
        if launched:
            try:
                with (metadata / "server.log").open("w") as log:
                    subprocess.run(["docker", "logs", name], stdout=log, stderr=subprocess.STDOUT, timeout=30)
                for source, filename in (("/state/postgres.log", "postgres.log"), ("/state/rabbit/server.log", "rabbitmq.log")):
                    subprocess.run(["docker", "cp", name + ":" + source, str(metadata / filename)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                # Normal graceful lifecycle only. Never use docker kill as a test.
                subprocess.run(["docker", "stop", "--time=60", name], check=True,
                               stdout=subprocess.DEVNULL, timeout=75)
                details = inspect(name)
                write_json(metadata / "container-final.json", details)
                if details["State"]["OOMKilled"] or details["State"]["ExitCode"]:
                    error = (error or "") + f"\nStack exited abnormally: {details['State']}"
                subprocess.run(["docker", "rm", "-v", name], check=True, stdout=subprocess.DEVNULL, timeout=30)
            except Exception:
                error = (error or "") + "\nCleanup/evidence error: " + traceback.format_exc()
        write_json(metadata / "status.json", {"error": error})
        path = a.output / label / "summary.json"
        if path.exists():
            summary = json.loads(path.read_text())
            if error:
                summary["status"] = "failed"
                summary["errors"].append(error)
            summary.update(broker=mode, profile=f"{profile[0]}x{profile[1]}", requested_rate=rate if mode != "baseline" else 0,
                           server_stack_metadata=metadata.name)
            write_json(path, summary)
        else:
            summary = {"label": label, "broker": mode, "status": "failed", "errors": [error or "no client summary"]}
            (a.output / label).mkdir(exist_ok=True)
            write_json(path, summary)
    return summary


def final_report(output, rows):
    fields = ["label", "broker", "profile", "rate", "status", "confirmed_publish_per_s", "completed_per_s",
              "publish_p95_ms", "end_to_end_p95_ms", "db_p95_ms", "baseline_db_p95_ms", "db_p95_increase_percent",
              "provisional_db_p95_le_10_percent", "offered_load_achieved_fraction", "duplicates", "missing_confirmed",
              "publish_failures", "backlog_max_client_outstanding", "backlog_max_confirmed_not_received",
              "backlog_confirmed_not_received_at_measurement_end"]
    with (output / "summary.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            flat = {k: row.get(k) for k in fields}
            flat.update(rate=row.get("requested_rate"), publish_p95_ms=row.get("publish", {}).get("p95_ms"),
                        end_to_end_p95_ms=row.get("end_to_end", {}).get("p95_ms"), db_p95_ms=row.get("synthetic_db", {}).get("p95_ms"))
            writer.writerow(flat)
    brokers = [row for row in rows if row.get("broker") != "baseline"]
    missed = [row["label"] for row in brokers if row.get("provisional_db_p95_le_10_percent") is False]
    under = [row["label"] for row in brokers if row.get("offered_load_achieved_fraction") is not None
             and row["offered_load_achieved_fraction"] < .95]
    failed = [row["label"] for row in rows if row["status"] != "passed"]
    (output / "report.txt").write_text(
        f"Recorded cells: {len(rows)}\nFailed measurement/accounting cells: {len(failed)}\n"
        f"Cells missing provisional synthetic-DB p95 <=10% criterion: {len(missed)}\n"
        f"Broker cells achieving <95% of requested offered load: {len(under)}\n"
        f"Failed: {failed}\nProvisional criterion missed: {missed}\nUnder target load: {under}\n\n"
        "Inspect all per-cell summaries, raw samples and environment details. A successful harness exit is not a performance qualification.\n"
        "These are paced finite-window synthetic experiments, not maximum capacity, real ERP, Win11 or power-loss qualification.\n"
        "Entire server stack shares 2 logical CPU / 4 GiB / no swap; clients use two separate host CPU IDs outside that budget.\n"
        "GitHub artifacts expire after 30 days; preserve complete evidence before expiration.\n")
    # Preserve all runs, including failures and under-achieved offered load.
    write_json(output / "MANIFEST.json", {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in sorted(output.rglob("*")) if p.is_file() and p.name != "MANIFEST.json"})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--image", required=True, help="already-built immutable image ID or local image tag")
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--warmup-seconds", type=float, default=30)
    p.add_argument("--duration-seconds", type=float, default=120)
    p.add_argument("--rates", type=float, nargs="+", default=[200, 400])
    a = p.parse_args()
    if a.repetitions < 1 or a.warmup_seconds < 0 or a.duration_seconds <= 0 or min(a.rates) <= 0:
        p.error("invalid repetitions, durations or rates")
    if a.output.exists():
        p.error("output must be new; refusing to replace evidence")
    available = sorted(os.sched_getaffinity(0))
    if len(available) < 4:
        p.error("requires at least four host logical CPUs: two server CPUs and two separate client CPUs")
    server_cpus, client_cpus = available[:2], available[2:4]
    a.output.mkdir(parents=True)
    image = command(["docker", "image", "inspect", a.image, "--format={{.Id}}"])
    environment = {"evidence_level": "clean GitHub Linux VM; not dedicated physical hardware or Win11 qualification",
                   "platform": platform.platform(), "logical_cpus": os.cpu_count(), "host_affinity": available,
                   "memory_bytes": psutil.virtual_memory().total, "server_cpu_ids": server_cpus,
                   "client_cpu_ids": client_cpus, "server_total_cpu_quota": 2, "server_total_memory_bytes": MEMORY_BYTES,
                   "server_swap_bytes": 0, "clients_outside_budget": True, "image_id": image, "base_image": BASE_IMAGE,
                   "commit": command(["git", "rev-parse", "HEAD"], cwd=REPO),
                   "dirty_worktree": command(["git", "status", "--porcelain"], cwd=REPO),
                   "docker_info": json.loads(command(["docker", "info", "--format={{json .}}"])),
                   "runner_image_version": os.environ.get("ImageVersion"), "runner_image_os": os.environ.get("ImageOS"),
                   "postgresql": "18.6", "rabbitmq": "4.0.5", "python_proton": "0.40.0",
                   "python_version": platform.python_version(),
                   "repetitions": a.repetitions, "warmup_seconds": a.warmup_seconds,
                   "duration_seconds": a.duration_seconds, "offered_rates": a.rates, "profiles": ["1x1", "4x4"],
                   "ordering": "fresh baseline per repetition; broker and profile order alternates; new server data per cell",
                   "storage": "fresh Docker named/anonymous volume on same runner; no physical SSD guarantee; host caches not cleared"}
    write_json(a.output / "environment.json", environment)
    (a.output / "python-inputs.txt").write_text(command([sys.executable, "-m", "pip", "freeze"]) + "\n")
    os.sched_setaffinity(0, client_cpus)
    rows = []
    try:
        for repetition in range(1, a.repetitions + 1):
            baseline = run_cell(a, image, f"r{repetition}-baseline", "baseline", (0, 0), 200, server_cpus)
            rows.append(baseline)
            final_report(a.output, rows)
            base_p95 = baseline.get("synthetic_db", {}).get("p95_ms")
            profiles = [(1, 1), (4, 4)] if repetition % 2 else [(4, 4), (1, 1)]
            brokers = ["echoo", "rabbitmq"] if repetition % 2 else ["rabbitmq", "echoo"]
            rates = a.rates if repetition % 2 else list(reversed(a.rates))
            for profile in profiles:
                for rate in rates:
                    for broker in brokers:
                        label = f"r{repetition}-{broker}-{profile[0]}x{profile[1]}-{rate:g}pps"
                        row = run_cell(a, image, label, broker, profile, rate, server_cpus)
                        row["baseline_label"] = baseline["label"]
                        row["baseline_db_p95_ms"] = base_p95
                        p95 = row.get("synthetic_db", {}).get("p95_ms")
                        increase = 100 * (p95 / base_p95 - 1) if p95 is not None and base_p95 and baseline["status"] == "passed" else None
                        row["db_p95_increase_percent"] = increase
                        row["provisional_db_p95_le_10_percent"] = increase <= 10 if increase is not None else None
                        write_json(a.output / label / "summary.json", row)
                        rows.append(row)
                        final_report(a.output, rows)
                        print(f"{label}: {row['status']}; synthetic DB p95 change={increase}", flush=True)
    finally:
        final_report(a.output, rows)
        os.sched_setaffinity(0, available)
    return any(row["status"] != "passed" for row in rows)


if __name__ == "__main__":
    raise SystemExit(main())
