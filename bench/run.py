#!/usr/bin/env python3
"""Reproducible single-producer AMQP 1.0 durable-confirm benchmark.

This is a capacity probe, not a claim about ERP or Win11 qualification.
The broker and queue must already exist. No benchmark turns durability off.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

import psutil
import psycopg
from proton import Delivery, Message, SSLDomain
from proton.utils import BlockingConnection


def distribution(values):
    ordered = sorted(values)
    def pct(p):
        return ordered[min(len(ordered)-1, int((len(ordered)-1)*p))] if ordered else None
    return {"count": len(values), "p50_ms": pct(.5), "p95_ms": pct(.95),
            "p99_ms": pct(.99), "mean_ms": statistics.mean(values) if values else None}


def connect(args):
    ssl = SSLDomain(SSLDomain.MODE_CLIENT)
    ssl.set_trusted_ca_db(args.ca)
    ssl.set_peer_authentication(SSLDomain.VERIFY_PEER_NAME)
    ssl.set_credentials(args.cert, args.key, None)
    return BlockingConnection(args.url, ssl_domain=ssl, timeout=args.timeout,
                              allowed_mechs=args.sasl, virtual_host=args.virtual_host,
                              sni=urlsplit(args.url).hostname)


def database_workload(args, stop, data):
    try:
        # A deliberately small synthetic inventory transaction. It is not ERP.
        with psycopg.connect(args.dsn, autocommit=True) as db:
            db.execute("CREATE SCHEMA IF NOT EXISTS echoo_benchmark")
            db.execute("CREATE TABLE IF NOT EXISTS echoo_benchmark.inventory (id int PRIMARY KEY, quantity bigint NOT NULL)")
            db.execute("INSERT INTO echoo_benchmark.inventory SELECT x, 100000 FROM generate_series(1,1000) x ON CONFLICT DO NOTHING")
            i = 0
            while not stop.is_set():
                start = time.perf_counter_ns()
                with db.transaction():
                    db.execute("UPDATE echoo_benchmark.inventory SET quantity=quantity+1 WHERE id=%s", (i % 1000+1,))
                    db.execute("SELECT sum(quantity) FROM echoo_benchmark.inventory WHERE id BETWEEN %s AND %s", (i % 900+1, i % 900+100)).fetchone()
                data["db_latency_ms"].append((time.perf_counter_ns()-start)/1e6)
                i += 1
                stop.wait(max(0., args.db_interval-(time.perf_counter_ns()-start)/1e9))
    except BaseException:
        data["errors"].append({"component": "synthetic_db", "trace": traceback.format_exc()})
        stop.set()


def sample_resources(args, stop, data):
    roots = [psutil.Process(pid) for pid in args.server_pid]
    observed = {}
    while not stop.is_set():
        processes = {}
        for root in roots:
            try:
                for proc in [root] + root.children(recursive=True):
                    processes[proc.pid] = proc
            except psutil.Error:
                pass
        rss, cpu = 0, 0.
        for pid, proc in processes.items():
            try:
                proc = observed.get(pid, proc)
                rss += proc.memory_info().rss
                cpu += proc.cpu_percent()
                observed[pid] = proc
            except psutil.Error:
                pass
        data["resource_samples"].append({"elapsed_s": time.monotonic()-data["start"],
                                         "sum_rss_bytes": rss, "sum_cpu_percent": cpu,
                                         "processes": len(processes)})
        stop.wait(.2)


def run(args):
    run_id = str(uuid.uuid4())
    data = {"run_id": run_id, "start": time.monotonic(), "publish_ms": [],
            "end_to_end_ms": [], "db_latency_ms": [], "resource_samples": [],
            "errors": [], "received_ids": [], "duplicate_count": 0}
    stop, ready = threading.Event(), threading.Event()
    threads = []
    if args.dsn:
        threads.append(threading.Thread(target=database_workload, args=(args, stop, data)))
    if args.server_pid:
        threads.append(threading.Thread(target=sample_resources, args=(args, stop, data)))
    for thread in threads:
        thread.start()
    started = time.monotonic()
    if args.baseline_seconds:
        stop.wait(args.baseline_seconds)
    else:
        payload = bytes(range(256))*(args.payload_bytes//256)+bytes(range(args.payload_bytes % 256))
        digest = hashlib.sha256(payload).hexdigest()
        def consume():
            seen = set()
            try:
                with closing(connect(args)) as connection:
                    receiver = connection.create_receiver(args.address, credit=args.credit)
                    ready.set()
                    while len(seen) < args.messages and not stop.is_set():
                        msg = receiver.receive(timeout=args.timeout)
                        if msg.properties.get("run") != run_id:
                            raise RuntimeError("queue is not empty or another run is using the address")
                        if hashlib.sha256(bytes(msg.body)).hexdigest() != digest:
                            raise RuntimeError("payload checksum mismatch")
                        if msg.id in seen:
                            data["duplicate_count"] += 1
                        else:
                            seen.add(msg.id)
                            data["received_ids"].append(str(msg.id))
                            data["end_to_end_ms"].append((time.perf_counter_ns()-msg.properties["sent_ns"])/1e6)
                        receiver.accept()
                    receiver.close()
            except BaseException:
                data["errors"].append({"component": "consumer", "trace": traceback.format_exc()})
                stop.set()
                ready.set()
        consumer = threading.Thread(target=consume)
        consumer.start()
        try:
            if not ready.wait(args.timeout) or stop.is_set():
                raise RuntimeError("consumer was not ready")
            with closing(connect(args)) as connection:
                sender = connection.create_sender(args.address)
                started = time.monotonic()
                for i in range(args.messages):
                    if stop.is_set():
                        break
                    before = time.perf_counter_ns()
                    delivery = sender.send(Message(body=payload, durable=True, id=f"{run_id}/{i}",
                                           properties={"run": run_id, "sent_ns": before}))
                    if delivery.remote_state != Delivery.ACCEPTED:
                        raise RuntimeError(f"non-accepted publisher outcome: {delivery.remote_state}")
                    data["publish_ms"].append((time.perf_counter_ns()-before)/1e6)
                data["publisher_elapsed_s"] = time.monotonic()-started
            consumer.join(args.timeout)
            if consumer.is_alive():
                raise RuntimeError("consumer did not finish before timeout")
        except BaseException:
            data["errors"].append({"component": "publisher", "trace": traceback.format_exc()})
        finally:
            stop.set()
            consumer.join(args.timeout+1)
    elapsed = time.monotonic()-started
    stop.set()
    for thread in threads:
        thread.join(args.timeout)
    summary = {"format_version": 1, "run_id": run_id, "label": args.label,
               "status": "failed" if data["errors"] else "passed",
               "host": {"platform": platform.platform(), "cpu_logical": os.cpu_count(),
                        "memory_bytes": psutil.virtual_memory().total},
               "conditions": {"payload_bytes": args.payload_bytes, "messages": args.messages,
                              "credit": args.credit, "producer_connections": 1, "consumer_connections": 1,
                              "confirmation": "per-message remote ACCEPTED; durable=True; manual consumer ACCEPTED",
                              "tls": "mutual TLS and server-name validation", "sasl": args.sasl,
                              "broker_version": args.broker_version, "queue_type": args.queue_type,
                              "synthetic_db": bool(args.dsn), "db_interval_s": args.db_interval,
                              "baseline_seconds": args.baseline_seconds},
               "elapsed_s": elapsed, "publish": distribution(data["publish_ms"]),
               "end_to_end": distribution(data["end_to_end_ms"]),
               "synthetic_db": distribution(data["db_latency_ms"]),
               "confirmed_publish_per_s": len(data["publish_ms"])/data.get("publisher_elapsed_s", elapsed),
               "completed_per_s": len(data["received_ids"])/elapsed,
               "duplicates": data["duplicate_count"], "errors": data["errors"],
               "resource_note": "RSS is summed across processes and double-counts shared pages; CPU process samples are indicative, not isolated accounting"}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output/f"{run_id}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    data.pop("start")
    (output/f"{run_id}.raw.json").write_text(json.dumps(data, ensure_ascii=False)+"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return bool(data["errors"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--url", default="amqps://localhost:5671")
    parser.add_argument("--address", default="bench")
    parser.add_argument("--ca")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--sasl", default="EXTERNAL")
    parser.add_argument("--virtual-host", default="localhost")
    parser.add_argument("--messages", type=int, default=1000)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--credit", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--dsn", default=os.environ.get("BENCH_PG_DSN"))
    parser.add_argument("--db-interval", type=float, default=.005)
    parser.add_argument("--baseline-seconds", type=float, default=0)
    parser.add_argument("--server-pid", type=int, nargs="*", default=[])
    parser.add_argument("--broker-version", required=True)
    parser.add_argument("--queue-type", required=True)
    parser.add_argument("--output", default="bench/results")
    options = parser.parse_args()
    if not options.baseline_seconds and not all((options.ca, options.cert, options.key)):
        parser.error("AMQP benchmark requires --ca --cert --key")
    if options.messages < 1 or options.credit < 1 or options.payload_bytes < 0:
        parser.error("messages and credit must be positive; payload-bytes must be nonnegative")
    raise SystemExit(run(options))
