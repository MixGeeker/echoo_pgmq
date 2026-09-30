"""Offline accounting checks. No broker fault, parser or security scenarios."""
import argparse
import gzip
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


duration = module("run_duration")
comparison = module("compare_ci")


def args():
    return argparse.Namespace(run_id="ordinary-test", label="unit", warmup_seconds=1,
                              duration_seconds=2, producers=1, consumers=1,
                              offered_rate=2, payload_bytes=1024, credit=32, db_interval=.005)


def evidence(path, name, rows):
    with gzip.open(path / name, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def test_distribution_and_half_open_windows():
    assert duration.distribution([])["p95_ms"] is None
    assert duration.distribution([3, 1, 2])["p50_ms"] == 2
    assert duration.in_window(1_000_000_000, 1_000_000_000, 2)
    assert not duration.in_window(3_000_000_000, 1_000_000_000, 2)


def test_warmup_excluded_and_slow_tail_kept(tmp_path):
    evidence(tmp_path, "producer-0.jsonl.gz", [
        {"id": "warm", "start_ns": 500_000_000, "complete_ns": 600_000_000, "accepted": True,
         "latency_ms": 100, "schedule_lateness_ms": 0},
        {"id": "measured", "start_ns": 2_900_000_000, "complete_ns": 3_100_000_000,
         "accepted": True, "latency_ms": 200, "schedule_lateness_ms": 0}])
    evidence(tmp_path, "consumer-0.jsonl.gz", [
        {"id": "warm", "sent_ns": 500_000_000, "received_ns": 600_000_000, "valid": True, "latency_ms": 100},
        {"id": "measured", "sent_ns": 2_900_000_000, "received_ns": 3_200_000_000, "valid": True, "latency_ms": 300}])
    evidence(tmp_path, "db-0.jsonl.gz", [
        {"start_ns": 500_000_000, "latency_ms": 99, "schedule_lateness_ms": 0},
        {"start_ns": 1_500_000_000, "latency_ms": 2, "schedule_lateness_ms": 0}])
    summary = duration.summarize(tmp_path, args(), 0, [], [])
    assert summary["status"] == "passed"
    assert summary["publish"]["count"] == 1
    assert summary["publish"]["p95_ms"] == 200  # tail completion beyond window is retained
    assert summary["end_to_end"]["p95_ms"] == 300
    assert summary["confirmed_publish_per_s"] == 0  # fixed-window event count
    assert summary["synthetic_db"]["p95_ms"] == 2
    assert summary["offered_load_achieved_fraction"] == .25


def test_identity_accounting_across_multiple_consumers(tmp_path):
    evidence(tmp_path, "producer-0.jsonl.gz", [
        {"id": "one", "start_ns": 1_100_000_000, "complete_ns": 1_200_000_000,
         "accepted": True, "latency_ms": 100, "schedule_lateness_ms": 0}])
    evidence(tmp_path, "consumer-0.jsonl.gz", [
        {"id": "one", "sent_ns": 1_100_000_000, "received_ns": 1_300_000_000, "valid": True, "latency_ms": 200}])
    evidence(tmp_path, "consumer-1.jsonl.gz", [
        {"id": "one", "sent_ns": 1_100_000_000, "received_ns": 1_400_000_000, "valid": True, "latency_ms": 300}])
    evidence(tmp_path, "db-0.jsonl.gz", [{"start_ns": 1_500_000_000, "latency_ms": 2, "schedule_lateness_ms": 0}])
    summary = duration.summarize(tmp_path, args(), 0, [], [])
    assert summary["status"] == "failed"
    assert summary["duplicates"] == 1
    assert summary["completed_per_s"] == .5  # no duplicate throughput inflation
    assert summary["missing_confirmed"] == 0


def test_whole_stack_budget_is_verified():
    comparison.verify_budget({"HostConfig": {"NanoCpus": 2_000_000_000,
        "Memory": 4 * 1024 ** 3, "MemorySwap": 4 * 1024 ** 3,
        "CpusetCpus": "0,1", "PidsLimit": 512}}, [0, 1])


def test_durable_settings_and_actual_cgroup_are_verified():
    comparison.verify_settings({"postgresql": {
        "fsync": "on", "full_page_writes": "on", "synchronous_commit": "on", "autovacuum": "on",
        "server_version": "18.6", "wal_level": "replica", "shared_buffers": "65536", "max_connections": "40",
        "max_wal_size": "1024", "checkpoint_timeout": "300"},
        "message_table_logged": True, "cgroup_v2": {
            "cpu.max": "200000 100000", "memory.max": str(4 * 1024 ** 3), "memory.swap.max": "0"}}, "echoo")


def test_wal_lsn_difference_across_segment_boundary():
    assert comparison.wal_bytes("1/00000000") - comparison.wal_bytes("0/FFFFFFFF") == 1


def test_docker_stats_closes_plain_http_connection(monkeypatch):
    # Like stdlib HTTPConnection, this object deliberately has no __enter__.
    calls = []
    class Response:
        status = 200
        def read(self):
            return b'{"cpu_stats":{"cpu_usage":{"total_usage":42}},"memory_stats":{"usage":123}}'
    class Connection:
        def __init__(self, host, timeout):
            calls.append(("open", host, timeout))
        def request(self, method, path):
            calls.append((method, path))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(duration, "DockerConnection", Connection)
    result = duration.container_stats("echoo-bench-normal")
    assert result["memory_stats"]["usage"] == 123
    assert calls == [("open", "localhost", 5),
                     ("GET", "/containers/echoo-bench-normal/stats?stream=false&one-shot=true"), ("close",)]


def test_setup_and_zero_samples_stop_the_matrix():
    usable = {"status": "passed", "broker": "echoo", "synthetic_db": {"count": 100},
              "publish": {"count": 100}, "measured_resource_sample_count": 10,
              "offered_load_achieved_fraction": .5, "provisional_db_p95_le_10_percent": False}
    # Genuine slow observations and missed provisional criteria must stay visible.
    assert not comparison.should_abort(usable)
    assert comparison.should_abort(dict(usable, status="failed", failure_kind="setup"))
    assert comparison.should_abort(dict(usable, status="failed", failure_kind="infrastructure"))
    assert comparison.should_abort(dict(usable, synthetic_db={"count": 0}))
    assert comparison.should_abort(dict(usable, measured_resource_sample_count=0))
    assert comparison.should_abort(dict(usable, publish={"count": 0}))
    assert comparison.should_abort(dict(usable, status="failed"), smoke=True)


def test_abort_records_remaining_cells_as_unrun(tmp_path):
    comparison.record_abort(tmp_path, [{"label": "r1-baseline"}], 27)
    result = json.loads((tmp_path / "matrix-status.json").read_text())
    assert result["recorded_cells"] == 1
    assert result["remaining_cells_not_run"] == 26
    assert result["status"] == "aborted"


def test_real_docker_smoke_is_before_the_full_matrix():
    workflow = (Path(__file__).parents[1] / ".github/workflows/benchmark.yml").read_text()
    assert workflow.index("Fail-fast real Docker smoke") < workflow.index("Warm up and measure identical paced workloads")
    assert workflow.index("Preserve infrastructure smoke evidence immediately") < workflow.index("Warm up and measure identical paced workloads")
    assert '--output "$RUNNER_TEMP/benchmark-infrastructure-smoke" --smoke' in workflow


@pytest.mark.parametrize("smoke,expected_cells", [(True, 3), (False, 27)])
def test_cli_stops_after_first_unusable_cell(tmp_path, monkeypatch, smoke, expected_cells):
    output = tmp_path / "new-results"
    argv = ["compare_ci.py", "--output", str(output), "--image", "unit-image"]
    if smoke:
        argv.append("--smoke")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(comparison.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    monkeypatch.setattr(comparison.os, "sched_setaffinity", lambda *_: None)
    def cmd(args, **_):
        if args[:2] == ["docker", "info"]:
            return "{}"
        return "unit-input"
    monkeypatch.setattr(comparison, "command", cmd)
    calls = []
    def unusable_cell(a, image, label, mode, profile, rate, server_cpus):
        calls.append(label)
        cell = output / label
        cell.mkdir()
        result = {"label": label, "broker": mode, "status": "failed", "failure_kind": "infrastructure",
                  "errors": ["offline accounting fixture"], "synthetic_db": {"count": 0}, "measured_resource_sample_count": 0}
        comparison.write_json(cell / "summary.json", result)
        return result
    monkeypatch.setattr(comparison, "run_cell", unusable_cell)
    assert comparison.main() == 1
    assert calls == ["r1-baseline"]
    status = json.loads((output / "matrix-status.json").read_text())
    assert status["expected_cells"] == expected_cells
    assert status["remaining_cells_not_run"] == expected_cells - 1
    assert (output / "r1-baseline/summary.json").exists()
    assert (output / "MANIFEST.json").exists()


def test_provisional_target_failures_are_preserved_in_csv(tmp_path):
    row = {"label": "normal", "broker": "echoo", "status": "passed",
           "synthetic_db": {"p95_ms": 1.587}, "baseline_db_p95_ms": .493,
           "db_p95_increase_percent": 221.9, "provisional_db_p95_le_10_percent": False}
    comparison.final_report(tmp_path, [row])
    text = (tmp_path / "summary.csv").read_text()
    assert "221.9,False" in text
    assert "summary.csv" in json.loads((tmp_path / "MANIFEST.json").read_text())
