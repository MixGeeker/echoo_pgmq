#!/usr/bin/env python3
"""Benign, paced AMQP 1.0 measurement against a disposable prepared stack.

Separate client processes stay outside the server container's shared budget.
This is a fixed offered-load experiment, not a maximum-capacity assertion.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import http.client
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import queue
import socket
import statistics
import time
import traceback
import uuid
from urllib.parse import urlsplit

import psutil
import psycopg
from proton import Delivery, Message, SSLDomain, Timeout
from proton.utils import BlockingConnection


def distribution(values):
    ordered = sorted(values)
    def percentile(p):
        return ordered[int((len(ordered) - 1) * p)] if ordered else None
    return {"count": len(values), "p50_ms": percentile(.50),
            "p95_ms": percentile(.95), "p99_ms": percentile(.99),
            "mean_ms": statistics.mean(values) if values else None}


def in_window(ns, start_ns, duration):
    return start_ns <= ns < start_ns + int(duration * 1e9)


def connection(a):
    ssl = SSLDomain(SSLDomain.MODE_CLIENT)
    ssl.set_trusted_ca_db(a.ca)
    ssl.set_peer_authentication(SSLDomain.VERIFY_PEER_NAME)
    ssl.set_credentials(a.cert, a.key, None)
    return BlockingConnection(a.url, ssl_domain=ssl, timeout=a.timeout,
                              allowed_mechs="EXTERNAL", virtual_host=a.virtual_host,
                              sni=urlsplit(a.url).hostname)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_rows(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


def paced_wait(due_ns, stop):
    stop.wait(max(0., (due_ns - time.perf_counter_ns()) / 1e9))


def worker(kind, index, a, ready, begin, stop, epoch, counters, directory):
    """Each process owns its connection and compressed append-only evidence."""
    path = Path(directory) / f"{kind}-{index}.jsonl.gz"
    failed = None
    try:
        with gzip.open(path, "wt") as raw:
            def emit(row):
                raw.write(json.dumps(row, separators=(",", ":")) + "\n")
            if kind == "db":
                with psycopg.connect(a.dsn, autocommit=True) as db:
                    ready.put((kind, index, None))
                    begin.wait()
                    due = epoch.value
                    transaction_index = 0
                    end = epoch.value + int((a.warmup_seconds + a.duration_seconds) * 1e9)
                    while not stop.is_set():
                        paced_wait(due, stop)
                        start = time.perf_counter_ns()
                        if start >= end or stop.is_set():
                            break
                        with db.transaction():
                            db.execute("UPDATE echoo_benchmark.inventory SET quantity=quantity+1 WHERE id=%s", (transaction_index % 1000 + 1,))
                            db.execute("SELECT sum(quantity) FROM echoo_benchmark.inventory WHERE id BETWEEN %s AND %s",
                                       (transaction_index % 900 + 1, transaction_index % 900 + 100)).fetchone()
                        complete = time.perf_counter_ns()
                        emit({"start_ns": start, "complete_ns": complete,
                              "latency_ms": (complete - start) / 1e6,
                              "schedule_lateness_ms": (start - due) / 1e6})
                        # Never build a catch-up burst when the database falls behind.
                        due = max(due + int(a.db_interval * 1e9), complete)
                        transaction_index += 1
            elif kind == "producer":
                payload = bytes(range(256)) * (a.payload_bytes // 256) + bytes(range(a.payload_bytes % 256))
                with closing(connection(a)) as conn:
                    sender = conn.create_sender(a.address)
                    ready.put((kind, index, None))
                    begin.wait()
                    interval = int(a.producers / a.offered_rate * 1e9)
                    due = epoch.value + int(index / a.offered_rate * 1e9)
                    end = epoch.value + int((a.warmup_seconds + a.duration_seconds) * 1e9)
                    sequence = 0
                    while not stop.is_set():
                        paced_wait(due, stop)
                        before = time.perf_counter_ns()
                        if before >= end or stop.is_set():
                            break
                        message_id = f"{a.run_id}/{index}/{sequence}"
                        with counters.get_lock():
                            counters[0] += 1
                        row = {"id": message_id, "start_ns": before,
                               "schedule_lateness_ms": (before - due) / 1e6}
                        try:
                            delivery = sender.send(Message(body=payload, durable=True, id=message_id,
                                                   properties={"run": a.run_id, "sent_ns": before}))
                            row["complete_ns"] = time.perf_counter_ns()
                            row["latency_ms"] = (row["complete_ns"] - before) / 1e6
                            row["accepted"] = delivery.remote_state == Delivery.ACCEPTED
                            if not row["accepted"]:
                                row["outcome"] = str(delivery.remote_state)
                            else:
                                with counters.get_lock():
                                    counters[1] += 1
                            emit(row)
                            if not row["accepted"]:
                                raise RuntimeError("publisher outcome was not ACCEPTED")
                        except BaseException:
                            if "accepted" not in row:
                                row.update(accepted=False, complete_ns=time.perf_counter_ns(), error=traceback.format_exc())
                                emit(row)
                            raise
                        sequence += 1
                        due = max(due + interval, row["complete_ns"])
                    sender.close()
            else:
                payload = bytes(range(256)) * (a.payload_bytes // 256) + bytes(range(a.payload_bytes % 256))
                expected = hashlib.sha256(payload).digest()
                with closing(connection(a)) as conn:
                    receiver = conn.create_receiver(a.address, credit=a.credit)
                    ready.put((kind, index, None))
                    begin.wait()
                    while not stop.is_set():
                        try:
                            message = receiver.receive(timeout=.5)
                        except Timeout:
                            continue
                        received = time.perf_counter_ns()
                        valid = (message.properties.get("run") == a.run_id and
                                 hashlib.sha256(bytes(message.body)).digest() == expected)
                        row = {"id": str(message.id), "received_ns": received,
                               "sent_ns": message.properties.get("sent_ns"), "valid": valid}
                        if valid:
                            row["latency_ms"] = (received - row["sent_ns"]) / 1e6
                        emit(row)
                        if not valid:
                            raise RuntimeError("unexpected run ID or payload checksum mismatch")
                        receiver.accept()  # Manual ACCEPTED, never auto-ack/fire-and-forget.
                        with counters.get_lock():
                            counters[2] += 1
                    receiver.close()
    except BaseException:
        failed = traceback.format_exc()
        ready.put((kind, index, failed))
        stop.set()
    finally:
        if kind == "producer":
            with counters.get_lock():
                counters[3] += 1
        write_json(Path(directory) / f"{kind}-{index}.status.json", {"error": failed})


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def container_stats(name):
    with DockerConnection("localhost", timeout=5) as conn:
        conn.request("GET", f"/containers/{name}/stats?stream=false&one-shot=true")
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Docker stats returned HTTP {response.status}")
        return json.loads(response.read())


def summarize(directory, a, epoch_ns, errors, samples):
    measurement_start = epoch_ns + int(a.warmup_seconds * 1e9)
    rows = {kind: [] for kind in ("producer", "consumer", "db")}
    for kind in rows:
        for path in directory.glob(f"{kind}-*.jsonl.gz"):
            rows[kind].extend(read_rows(path))
    def measured(ns):
        return in_window(ns, measurement_start, a.duration_seconds)
    sent = {r["id"] for r in rows["producer"]}
    confirmed = {r["id"] for r in rows["producer"] if r["accepted"]}
    valid = [r for r in rows["consumer"] if r["valid"]]
    received = {r["id"] for r in valid}
    missing, unexpected = confirmed - received, received - sent
    duplicates = len(valid) - len(received)
    # Reconstruct exact application backlog from IDs, including delivery-before-
    # publisher-confirm races. A live counter is only a protective approximation.
    events = [(r["start_ns"], "attempt", r["id"]) for r in rows["producer"]]
    events += [(r["complete_ns"], "accepted", r["id"]) for r in rows["producer"] if r["accepted"]]
    events += [(r["received_ns"], "received", r["id"]) for r in valid]
    events.sort()
    attempted_ids, accepted_ids, delivered_ids = set(), set(), set()
    backlog_rows = []
    position = 0
    for at_ns in range(epoch_ns, epoch_ns + int((a.warmup_seconds + a.duration_seconds) * 1e9) + 1, 1_000_000_000):
        while position < len(events) and events[position][0] <= at_ns:
            _, event, message_id = events[position]
            if event == "attempt":
                attempted_ids.add(message_id)
            elif event == "accepted":
                accepted_ids.add(message_id)
            else:
                delivered_ids.add(message_id)
            position += 1
        backlog_rows.append({"at_ns": at_ns, "elapsed_s": (at_ns - epoch_ns) / 1e9,
                             "attempted_not_received": len(attempted_ids - delivered_ids),
                             "confirmed_not_received": len(accepted_ids - delivered_ids)})
    write_json(directory / "backlog.json", backlog_rows)
    pub = [r for r in rows["producer"] if measured(r["start_ns"]) and r["accepted"]]
    consume = [r for r in valid if measured(r["sent_ns"])]
    db = [r for r in rows["db"] if measured(r["start_ns"])]
    if missing or unexpected or duplicates or any(not r["valid"] for r in rows["consumer"]):
        errors.append("message identity/checksum accounting did not reconcile")
    if any(not r["accepted"] for r in rows["producer"]):
        errors.append("one or more publish attempts were not confirmed")
    if not db:
        errors.append("no measured synthetic database transactions")
    write_json(directory / "accounting.json", {"missing_confirmed_ids": sorted(missing),
               "unexpected_ids": sorted(unexpected), "received_without_confirm_ids": sorted(received - confirmed)})
    # Count throughput by event timestamp in the fixed window. Latencies are a
    # send-start cohort, including its delayed tail, so slow completions aren't dropped.
    pub_events = sum(r["accepted"] and measured(r["complete_ns"]) for r in rows["producer"])
    completed = {r["id"] for r in valid if measured(r["received_ns"])}
    attempted = sum(measured(r["start_ns"]) for r in rows["producer"])
    return {"format_version": 2, "run_id": a.run_id, "label": a.label,
            "status": "failed" if errors else "passed", "errors": errors,
            "measurement_start_ns": measurement_start, "duration_seconds": a.duration_seconds,
            "warmup_seconds": a.warmup_seconds, "conditions": {
                "producers": a.producers, "consumers": a.consumers,
                "offered_rate_per_s": a.offered_rate if a.producers else 0, "payload_bytes": a.payload_bytes,
                "credit_per_consumer": a.credit, "db_interval_s": a.db_interval,
                "publisher_inflight_per_connection": 1,
                "tls": "mTLS; server name verified", "sasl": "EXTERNAL",
                "settlement": "durable=True; wait remote ACCEPTED; manual consumer ACCEPTED",
                "end_to_end_latency": "send start to verified receipt, not consumer ACK commit latency",
                "pacing": "aggregate fixed target; no catch-up bursts; under-achievement reported",
                "client_location": "host processes, outside the shared server container budget"},
            "publish": distribution([r["latency_ms"] for r in pub]),
            "end_to_end": distribution([r["latency_ms"] for r in consume]),
            "synthetic_db": distribution([r["latency_ms"] for r in db]),
            "db_schedule_lateness": distribution([r["schedule_lateness_ms"] for r in db]),
            "publish_schedule_lateness": distribution([r["schedule_lateness_ms"] for r in pub]),
            "confirmed_publish_per_s": pub_events / a.duration_seconds,
            "completed_per_s": len(completed) / a.duration_seconds,
            "attempted_publish_per_s": attempted / a.duration_seconds,
            "offered_load_achieved_fraction": attempted / (a.offered_rate * a.duration_seconds) if a.producers else None,
            "synthetic_db_transactions_per_s": len(db) / a.duration_seconds,
            "duplicates": duplicates, "missing_confirmed": len(missing),
            "unexpected_received": len(unexpected),
            "publish_failures": sum(not r["accepted"] for r in rows["producer"]),
            "checksum_or_run_failures": sum(not r["valid"] for r in rows["consumer"]),
            "all_phases": {"attempted": len(sent), "accepted": len(confirmed), "received_unique": len(received)},
            "backlog_note": "backlog.json reconstructs exact ID-based application outstanding/confirmed-not-received once per second; broker ready/unacked depth is verified before/after only",
            "backlog_max_confirmed_not_received": max((r["confirmed_not_received"] for r in backlog_rows), default=0),
            "backlog_confirmed_not_received_at_measurement_end": backlog_rows[-1]["confirmed_not_received"] if backlog_rows else None,
            "backlog_max_client_outstanding": max((s["client_outstanding"] for s in samples), default=0),
            "raw_evidence": "per-client *.jsonl.gz, resources.jsonl.gz, accounting.json, backlog.json; warmup and drain retained"}


def run(a):
    a.run_id = str(uuid.uuid4())
    directory = Path(a.output) / a.label
    directory.mkdir(parents=True, exist_ok=False)
    context = mp.get_context("spawn")
    ready, begin, stop = context.Queue(), context.Event(), context.Event()
    epoch, counters = context.Value("q", 0), context.Array("q", [0, 0, 0, 0])
    processes, errors, samples = [], [], []
    try:
        with psycopg.connect(a.dsn, autocommit=True) as db:
            db.execute("CREATE SCHEMA IF NOT EXISTS echoo_benchmark")
            db.execute("CREATE TABLE IF NOT EXISTS echoo_benchmark.inventory (id int PRIMARY KEY, quantity bigint NOT NULL)")
            db.execute("INSERT INTO echoo_benchmark.inventory SELECT x,100000 FROM generate_series(1,1000) x ON CONFLICT DO NOTHING")
        for kind, count in (("db", 1), ("consumer", a.consumers), ("producer", a.producers)):
            for i in range(count):
                process = context.Process(target=worker, args=(kind, i, a, ready, begin, stop, epoch, counters, directory))
                process.start()
                processes.append(process)
        for _ in processes:
            kind, index, error = ready.get(timeout=a.timeout)
            if error:
                raise RuntimeError(f"{kind} {index} failed during setup: {error}")
        epoch.value = time.perf_counter_ns() + 200_000_000
        begin.set()
        finish_ns = epoch.value + int((a.warmup_seconds + a.duration_seconds) * 1e9)
        deadline = finish_ns + int(a.drain_seconds * 1e9)
        quiet_since = None
        with gzip.open(directory / "resources.jsonl.gz", "wt") as evidence:
            while not stop.is_set():
                now = time.perf_counter_ns()
                with counters.get_lock():
                    attempted, accepted, delivered, done = counters[:]
                sample = {"at_ns": now, "elapsed_s": (now - epoch.value) / 1e9,
                          "attempted": attempted, "accepted": accepted, "delivered": delivered,
                          "client_outstanding": max(0, attempted - delivered)}
                client = psutil.Process()
                cpu, rss = 0., 0
                for proc in [client] + client.children(recursive=True):
                    try:
                        t = proc.cpu_times()
                        cpu += t.user + t.system
                        rss += proc.memory_info().rss
                    except psutil.Error:
                        pass
                sample.update(client_cpu_seconds_sum=cpu, client_rss_bytes_sum=rss)
                if a.container:
                    sample["server_container"] = container_stats(a.container)
                samples.append({k: v for k, v in sample.items() if k != "server_container"})
                evidence.write(json.dumps(sample, separators=(",", ":")) + "\n")
                evidence.flush()
                if sample["client_outstanding"] > a.max_outstanding:
                    raise RuntimeError("benign load guard: outstanding messages exceeded configured ceiling")
                if now >= finish_ns and done == a.producers and delivered >= attempted:
                    quiet_since = quiet_since or now
                    if now - quiet_since >= 1_000_000_000:
                        break
                else:
                    quiet_since = None
                if now >= deadline:
                    raise RuntimeError("drain deadline exceeded; preserve and report backlog")
                stop.wait(.5)
    except BaseException:
        errors.append(traceback.format_exc())
    finally:
        stop.set()
        begin.set()
        for process in processes:
            process.join(a.timeout + 2)
            if process.is_alive():
                # Only stop an unresponsive benchmark CLIENT, never crash a server.
                process.terminate()
                process.join(5)
                errors.append(f"benchmark client {process.pid} did not exit normally")
            if process.exitcode:
                errors.append(f"benchmark client {process.pid} exit code {process.exitcode}")
        for status in directory.glob("*.status.json"):
            error = json.loads(status.read_text())["error"]
            if error:
                errors.append(error)
        try:
            summary = summarize(directory, a, epoch.value, errors, samples)
        except Exception:
            summary = {"format_version": 2, "label": a.label, "status": "failed",
                       "errors": errors + [traceback.format_exc()]}
        summary["client_host"] = {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                                  "affinity": psutil.Process().cpu_affinity() if hasattr(psutil.Process(), "cpu_affinity") else None,
                                  "memory_bytes": psutil.virtual_memory().total}
        write_json(directory / "summary.json", summary)
        print(json.dumps(summary, indent=2))
    return summary["status"] != "passed"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True)
    p.add_argument("--dsn", required=True)
    p.add_argument("--url", default="amqps://localhost:5671")
    p.add_argument("--address", default="bench")
    p.add_argument("--virtual-host", default="localhost")
    for name in ("ca", "cert", "key", "container"):
        p.add_argument(f"--{name}")
    p.add_argument("--output", required=True)
    p.add_argument("--producers", type=int, default=1)
    p.add_argument("--consumers", type=int, default=1)
    p.add_argument("--offered-rate", type=float, default=200)
    p.add_argument("--warmup-seconds", type=float, default=30)
    p.add_argument("--duration-seconds", type=float, default=120)
    p.add_argument("--drain-seconds", type=float, default=30)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--db-interval", type=float, default=.005)
    p.add_argument("--payload-bytes", type=int, default=1024)
    p.add_argument("--credit", type=int, default=32)
    p.add_argument("--max-outstanding", type=int, default=10000)
    return p


if __name__ == "__main__":
    p = parser()
    options = p.parse_args()
    if min(options.duration_seconds, options.offered_rate, options.drain_seconds,
           options.timeout, options.db_interval, options.credit, options.max_outstanding) <= 0:
        p.error("durations, rate, interval, credit and outstanding ceiling must be positive")
    if options.warmup_seconds < 0 or options.payload_bytes < 1 or min(options.producers, options.consumers) < 0:
        p.error("invalid payload, warmup or client count")
    if bool(options.producers) != bool(options.consumers):
        p.error("baseline uses 0 producers and 0 consumers; message runs need both")
    if options.producers and not all((options.ca, options.cert, options.key)):
        p.error("message runs require --ca, --cert and --key")
    raise SystemExit(run(options))
