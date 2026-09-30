#!/usr/bin/env python3
"""Materialize diagnostic-only A/B trees; never alter a product checkout.

A is the restored native baseline; B is the withdrawn native wake experiment.
Everything except src/echoo_pgmq.c comes from A. Every instrumentation transform
is applied identically and the transformed A/B delta must equal the known native
wake delta after instrumentation is removed. No product build files are edited.
"""
from __future__ import annotations

import argparse
import hashlib
import gzip
from collections import defaultdict
from bisect import bisect_right
import json
from pathlib import Path
import subprocess

BASELINE = "6867f2b280730634531d00f23043795165df8dd5"
WAKE = "472e62698137d0e1035d04775d443f1cbcc1ae33"
NATIVE = "src/echoo_pgmq.c"
BUILD_INPUTS = ("CMakeLists.txt", "echoo_pgmq.control", "include/", "src/", "sql/")


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), *args])


def replace_once(source: str, old: str, new: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"instrumentation anchor expected once, found {count}: {old[:100]!r}")
    return source.replace(old, new, 1)


# One immutable transform list is used for BOTH source variants. Keep old anchors
# disjoint from the known wake delta so there is no instrumentation interaction.
TRANSFORMS = (
    ("    pn_delivery_t *delivery;\n    int64 id;", "    pn_delivery_t *delivery;\n    uint64_t diag_delivery;\n    int64 id;"),
    ("    pn_link_t *link;\n    char *queue;", "    pn_link_t *link;\n    uint64_t diag_id, diag_publish_delivery, diag_publish_operation, diag_claim_operation;\n    char *queue;"),
    ("    pgsocket socket;\n    pn_connection_driver_t driver;", "    pgsocket socket;\n    uint64_t diag_id, diag_send_sequence;\n    pn_connection_driver_t driver;"),
    ("static int connection_count = 0;", 'static int connection_count = 0;\n\n#include "echoo_diagnostic_native_trace.h"'),
    ("else if (echoo_db_publish(link->queue, connection->identity,\n                              link->incoming, link->incoming_size))", "else if (echoo_diag_publish(connection, link, delivery))"),
    ("        pn_delivery_update(delivery, PN_ACCEPTED);", "        pn_delivery_update(delivery, PN_ACCEPTED);\n        echoo_diag_record(\"accepted_queued\", connection->diag_id, link->diag_id,\n                          link->diag_publish_delivery, link->diag_publish_operation, 0, 0, 0, 0, 1);"),
    ("if (!echoo_db_settle(link->queue, connection->identity, &link->owner,\n                         receipt->id, receipt->generation, outcome))", "if (!echoo_diag_settle(connection, link, receipt, outcome))"),
    ("    pn_link_open(pnlink);", "    pn_link_open(pnlink);\n    echoo_diag_link_open(connection, link);"),
    ("    result = echoo_db_claim(link->queue, connection->identity, &link->owner,\n                            visibility_seconds, &message);", "    result = echoo_diag_claim(connection, link, &message);"),
    ("    receipt->id = message.id;", "    receipt->diag_delivery = ++echoo_diag_delivery;\n    echoo_diag_delivery_map(connection, link, receipt->delivery, receipt->diag_delivery,\n                            link->diag_claim_operation, message.id, message.generation);\n    receipt->id = message.id;"),
    ("    pn_link_advance(link->link);\n    link->next_poll = 0;", "    pn_link_advance(link->link);\n    echoo_diag_record(\"transfer_queued\", connection->diag_id, link->diag_id,\n                      receipt->diag_delivery, link->diag_claim_operation, receipt->id, receipt->generation, 0, 0, 1);\n    link->next_poll = 0;"),
    ("size = send(connection->socket, buffer.start, (int) Min(buffer.size, (size_t) ECHOO_IO_PER_TURN), 0);", "size = echoo_diag_send(connection, buffer.start, (int) Min(buffer.size, (size_t) ECHOO_IO_PER_TURN), 0);"),
    ("        ++connection_count;", "        ++connection_count;\n        echoo_diag_connection_open(connection);"),
    ("    (void) main_arg;", "    (void) main_arg;\n    echoo_diag_init();"),
    ("        ResetLatch(MyLatch);", "        echoo_diag_loop_start();\n        ResetLatch(MyLatch);"),
    ("            int handled = 0;", "            int handled = 0;\n            echoo_diag_record(\"connection_visit\", connection->diag_id, 0, 0, 0, 0, 0, ++echoo_diag_visit, 0, 0);"),
    ("                    pump_sender(connection, link, now);", "                    echoo_diag_record(\"sender_visit\", connection->diag_id, link->diag_id, 0, 0,\n                                      0, 0, link->next_poll, link->inflight, pn_link_credit(link->link));\n                    pump_sender(connection, link, now);"),
    ("        count = WaitEventSetWait(waitset, timeout, events, max_connections + 2, PG_WAIT_EXTENSION);", "        echoo_diag_record(\"wait_start\", 0, 0, 0, 0, 0, 0, timeout, 0, 0);\n        count = WaitEventSetWait(waitset, timeout, events, max_connections + 2, PG_WAIT_EXTENSION);\n        echoo_diag_record(\"wait_return\", 0, 0, 0, 0, 0, 0, timeout, 0, count);"),
    ("    int size;\n    if (events & WL_SOCKET_READABLE)", "    int size;\n    echoo_diag_record(\"io_visit\", connection->diag_id, 0, 0, 0, 0, 0, events, 0, 0);\n    if (events & WL_SOCKET_READABLE)"),
    ("free_connection(EchooConnection *connection)\n{", "free_connection(EchooConnection *connection)\n{\n    echoo_diag_record(\"connection_close\", connection->diag_id, 0, 0, 0, 0, 0, 0, 0, 0);"),
    ("    pn_ssl_domain_free(tls_domain);\n    proc_exit(0);", "    pn_ssl_domain_free(tls_domain);\n    echoo_diag_flush_normal_shutdown();\n    proc_exit(0);"),
)


def instrument(source: str) -> str:
    for old, new in TRANSFORMS:
        source = replace_once(source, old, new)
    return source


def uninstrument(source: str) -> str:
    for old, new in reversed(TRANSFORMS):
        source = replace_once(source, new, old)
    return source


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def materialize(repository: Path, output_root: Path) -> dict:
    repository = repository.resolve()
    output_root = output_root.resolve()
    if output_root == repository or repository in output_root.parents:
        raise ValueError("output must be outside the source checkout")
    if output_root.exists():
        raise ValueError("output root must not already exist")
    originals = {"A": git(repository, "show", f"{BASELINE}:{NATIVE}"),
                 "B": git(repository, "show", f"{WAKE}:{NATIVE}")}
    transformed = {label: instrument(data.decode()).encode() for label, data in originals.items()}
    for label in ("A", "B"):
        if uninstrument(transformed[label].decode()).encode() != originals[label]:
            raise AssertionError("instrumentation changed native product code")
    # No non-native runtime/header changes exist between the fixed revisions.
    non_native_diff = git(repository, "diff", "--name-only", BASELINE, WAKE, "--", "src", "include").decode().splitlines()
    if non_native_diff != [NATIVE]:
        raise ValueError(f"unexpected A/B runtime changes: {non_native_diff}")
    paths = git(repository, "ls-tree", "-r", "--name-only", BASELINE, "--", *BUILD_INPUTS).decode().splitlines()
    header = Path(__file__).with_name("native_trace.h").read_bytes()
    sources = {path: git(repository, "show", f"{BASELINE}:{path}") for path in paths}
    manifest = {"schema": 1, "baseline_commit": BASELINE, "wake_commit": WAKE,
                "runtime_difference": NATIVE, "instrumentation_identical": True,
                "diagnostic_only": True, "test_hooks": "OFF", "platform": "Linux",
                "header_sha256": sha256(header), "generator_sha256": sha256(Path(__file__).read_bytes()),
                "trace_env": "ECHOO_DIAGNOSTIC_TRACE_DIR", "variants": {}}
    output_root.mkdir(parents=True)
    for label in ("A", "B"):
        files = dict(sources)
        files[NATIVE] = transformed[label]
        files["src/echoo_diagnostic_native_trace.h"] = header
        manifest["variants"][label] = {"original_native_sha256": sha256(originals[label]), "files": {}}
        for path, data in files.items():
            destination = output_root / label / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            manifest["variants"][label]["files"][path] = sha256(data)
    differing = [path for path in manifest["variants"]["A"]["files"]
                 if manifest["variants"]["A"]["files"][path] != manifest["variants"]["B"]["files"][path]]
    if differing != [NATIVE]:
        raise AssertionError(f"generated A/B file differences: {differing}")
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest



def read_trace(path: Path) -> dict:
    """Read one normal-shutdown trace; malformed/missing files raise ValueError."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError) as error:
        raise ValueError(f"native trace unreadable: {error}") from error
    if len(records) < 3 or records[0].get("record") != "metadata" or records[-1].get("record") != "footer":
        raise ValueError("native trace lacks metadata/events/normal-shutdown footer")
    if any(row.get("record") != "event" for row in records[1:-1]):
        raise ValueError("native trace contains unexpected record kinds")
    return {"metadata": records[0], "events": records[1:-1], "footer": records[-1]}


def _validate_native_trace(path: Path, require_producer_before_consumer: bool = True,
                           trace_data: dict | None = None) -> dict:
    """Fail insufficient on loss/error/incomplete stages and validate observed order.

    Identity is deliberately external: controller must additionally join EVERY
    client message ID to its exact (link name, delivery tag) native delivery_map.
    Socket sends are connection-level observations, not message wire timestamps.
    This validator never compares clocks or claims a latency attribution.
    """
    problems = []
    try:
        trace = trace_data if trace_data is not None else read_trace(path)
    except ValueError as error:
        return {"status": "insufficient", "problems": [str(error)]}
    meta, rows, footer = trace["metadata"], trace["events"], trace["footer"]
    if meta.get("schema") != 1 or meta.get("clock") != "CLOCK_MONOTONIC" or meta.get("unit") != "ns":
        problems.append("unrecognized schema or clock")
    if not meta.get("clock_resolution_ns", 0) > 0:
        problems.append("missing clock resolution")
    if footer.get("normal_shutdown") is not True or footer.get("complete") is not True:
        problems.append("normal complete shutdown is not proven")
    for name in ("dropped_events", "clock_failures", "identity_failures", "observation_failures"):
        if footer.get(name) != 0:
            problems.append(f"{name} is nonzero or missing")
    if footer.get("events") != len(rows) or footer.get("attempted_events") != len(rows):
        problems.append("event totals disagree")
    if not rows or rows[0].get("event") != "worker_start" or rows[-1].get("event") != "normal_shutdown":
        problems.append("worker lifecycle markers are missing")
    previous_time = 0
    connections, links, deliveries = {}, {}, {}
    operations, sends, loops = {}, {}, {}
    stage_elapsed_ns = {"publish": [], "claim": [], "settle": []}
    sends_observed = 0
    send_sequence = defaultdict(int)
    first_publish_loop = None
    for index, row in enumerate(rows, 1):
        if row.get("seq") != index:
            problems.append("non-contiguous event sequence")
        timestamp = row.get("t_ns", 0)
        if not isinstance(timestamp, int) or timestamp <= 0 or timestamp < previous_time:
            problems.append("missing or non-monotonic event clock")
        previous_time = timestamp if isinstance(timestamp, int) else previous_time
        kind = row.get("event", "")
        try:
            data = bytes.fromhex(row.get("data_hex", ""))
            if len(data) > meta.get("identity_limit_bytes", 0):
                raise ValueError("oversized identity")
        except (ValueError, TypeError):
            problems.append("invalid identity bytes")
            data = b""
        if kind == "connection_open":
            if row["connection"] in connections or row["result"] != 1 or row["value1"] <= 0 or row["value2"] <= 0:
                problems.append("invalid/duplicate connection endpoint mapping")
            connections[row["connection"]] = row
        elif kind == "link_open":
            if row["link"] in links or row["result"] != 1 or row["connection"] not in connections:
                problems.append("invalid/duplicate link mapping")
            links[row["link"]] = row
        elif kind == "delivery_map":
            if row["delivery"] in deliveries or row["result"] != 1 or row["link"] not in links:
                problems.append("invalid/duplicate delivery mapping")
            deliveries[row["delivery"]] = row
        elif kind in ("publish_start", "claim_start", "settle_start"):
            stage = kind.removesuffix("_start")
            if row["operation"] in operations:
                problems.append("duplicate operation start")
            operations[row["operation"]] = row
            if stage == "publish" and first_publish_loop is None:
                first_publish_loop = row["loop"]
        elif kind in ("publish_return", "claim_return", "settle_return"):
            stage = kind.removesuffix("_return")
            start = operations.pop(row["operation"], None)
            if not start or start["event"] != stage + "_start" or any(start[key] != row[key] for key in ("connection", "link", "delivery")):
                problems.append("unmatched stage return")
            else:
                stage_elapsed_ns[stage].append(row["t_ns"] - start["t_ns"])
            if row["result"] not in ((0, 1) if stage == "claim" else (1,)):
                problems.append(f"unsuccessful {stage} return")
        elif kind in ("accepted_queued", "transfer_queued"):
            if row["delivery"] not in deliveries:
                problems.append("queued event lacks exact delivery mapping")
        elif kind == "socket_send_start":
            key = (row["connection"], row["value1"])
            if key in sends or row["connection"] not in connections or row["value2"] <= 0:
                problems.append("invalid socket send start")
            if row["value1"] != send_sequence[row["connection"]] + 1:
                problems.append("non-contiguous owned socket-send sequence")
            send_sequence[row["connection"]] = row["value1"]
            sends[key] = row
        elif kind == "socket_send_return":
            key = (row["connection"], row["value1"])
            start = sends.pop(key, None)
            if not start or start["value2"] != row["value2"] or row["result"] > row["value2"]:
                problems.append("unmatched socket send return")
            sends_observed += 1
        elif kind == "connection_visit":
            order = loops.setdefault(row["loop"], [])
            if row["value1"] != len(order) + 1 or row["connection"] in order:
                problems.append("invalid connection traversal ordinal")
            order.append(row["connection"])
        if kind in ("connection_open", "socket_send_start", "socket_send_return"):
            if row.get("tcp_nodelay") not in (0, 1) or row.get("observation_errno") != 0:
                problems.append("TCP_NODELAY observation missing or failed")
    if operations or sends:
        problems.append("unfinished native stage/socket span")
    producers = {row["connection"] for row in links.values() if row["value1"] == 0}
    consumers = {row["connection"] for row in links.values() if row["value1"] == 1}
    comparable_loops = 0
    reversed_loops = 0
    for loop, order in loops.items():
        # Start after first publication's loop: a full ready, steady-state turn.
        if first_publish_loop is None or loop <= first_publish_loop:
            continue
        if producers and consumers and producers | consumers <= set(order):
            comparable_loops += 1
            if max(order.index(item) for item in producers) >= min(order.index(item) for item in consumers):
                reversed_loops += 1
    if require_producer_before_consumer and (not comparable_loops or reversed_loops):
        problems.append("consistent producer-before-consumer traversal not proven")
    if not stage_elapsed_ns["publish"] or not stage_elapsed_ns["settle"] or not sends_observed:
        problems.append("no completed full message path/socket sends")
    return {"status": "sufficient_native_trace" if not problems else "insufficient",
            "problems": sorted(set(problems)), "event_count": len(rows),
            "worker_pid": meta.get("pid"),
            "clock": meta.get("clock"), "clock_scope": "native server elapsed only",
            "client_identity_join_required": True,
            "connections": len(connections), "links": len(links), "deliveries": len(deliveries),
            "socket_send_calls": sends_observed,
            "producer_before_consumer": {"comparable_loops": comparable_loops, "reversed_loops": reversed_loops},
            "stage_elapsed_ns": stage_elapsed_ns,
            "footer": footer}



def validate_trace(path: Path, client_directory: Path | None = None,
                   require_producer_before_consumer: bool = True) -> dict:
    """Validate native trace and exact client-ID joins; never infer missing joins.

    Returns complete/errors for the controller. Raw server timestamps in message
    rows share this one process's CLOCK_MONOTONIC clock; client clocks are NEVER
    subtracted. First-following-send elapsed is an observation only: TLS/socket
    batching prevents assigning its bytes or completion to a particular message.
    """
    try:
        trace = read_trace(path)
        result = _validate_native_trace(path, require_producer_before_consumer, trace)
        errors = result.pop("problems")
        result.update(complete=False, errors=errors)
        if errors:
            return result
        if client_directory is None:
            errors.append("client evidence directory is required for exact message-ID joins")
            result["status"] = "insufficient"
            return result
        rows = trace["events"]
        by_kind = defaultdict(list)
        for row in rows:
            by_kind[row["event"]].append(row)
        links = {row["link"]: row for row in by_kind["link_open"]}
        connections = {row["connection"]: row for row in by_kind["connection_open"]}
        native_tags = defaultdict(list)
        for row in by_kind["delivery_map"]:
            native_tags[(row["link"], row["data_hex"])].append(row)
        events_by_delivery = defaultdict(dict)
        events_by_operation = defaultdict(dict)
        for row in rows:
            if row["delivery"]:
                events_by_delivery[row["delivery"]][row["event"]] = row
            if row["operation"]:
                events_by_operation[row["operation"]][row["event"]] = row
        clients = {"producer": {}, "consumer": {}}
        endpoint_observations = []
        joined_native_deliveries = set()
        joined_native_links = set()
        client_connections = {"producer": set(), "consumer": set()}
        for kind in ("producer", "consumer"):
            readiness = sorted(Path(client_directory).glob(f"{kind}-*.ready.json"))
            if not readiness:
                errors.append(f"missing {kind} readiness evidence")
            for ready_path in readiness:
                ready = json.loads(ready_path.read_text())
                name_hex = ready["link_name"].encode().hex()
                matches = [row for row in links.values() if row["data_hex"] == name_hex]
                if len(matches) != 1:
                    errors.append(f"{ready_path.name}: exact native link-name mapping missing/ambiguous")
                    continue
                link = matches[0]
                joined_native_links.add(link["link"])
                connection = connections[link["connection"]]
                expected_role = 0 if kind == "producer" else 1
                if link["value1"] != expected_role:
                    errors.append(f"{ready_path.name}: native/client roles differ")
                layout = ready.get("network_layout", "same_network_namespace")
                if layout == "same_network_namespace":
                    if connection["value1"] != ready["peer"][1] or connection["value2"] != ready["local"][1]:
                        errors.append(f"{ready_path.name}: direct native/client socket endpoints differ")
                elif layout != "docker_loopback_port_forwarding":
                    errors.append(f"{ready_path.name}: unknown network layout")
                endpoint_observations.append({"kind": kind, "link_name": ready["link_name"],
                                              "native_connection": connection["connection"],
                                              "native_local_port": connection["value1"],
                                              "native_peer_port": connection["value2"],
                                              "client_local": ready["local"], "client_peer": ready["peer"],
                                              "network_layout": layout,
                                              "identity_join": "unique controlled AMQP link name plus exact delivery tag"})
                client_connections[kind].add(connection["connection"])
                raw_path = ready_path.with_name(ready_path.name.replace(".ready.json", ".jsonl.gz"))
                with gzip.open(raw_path, "rt") as stream:
                    client_rows = [json.loads(line) for line in stream if line.strip()]
                for client in client_rows:
                    identity = client["id"]
                    if identity in clients[kind]:
                        errors.append(f"duplicate {kind} message ID")
                        continue
                    if (kind == "producer" and client.get("accepted") is not True) or (kind == "consumer" and client.get("valid") is not True):
                        errors.append(f"unsuccessful {kind} client event")
                    delivery_maps = native_tags[(link["link"], client["delivery_tag_hex"])]
                    if len(delivery_maps) != 1:
                        errors.append(f"{kind} message exact delivery-tag mapping missing/ambiguous")
                        continue
                    delivery_map = delivery_maps[0]
                    joined_native_deliveries.add(delivery_map["delivery"])
                    clients[kind][identity] = {"client": client, "map": delivery_map}
        if set(clients["producer"]) != set(clients["consumer"]) or not clients["producer"]:
            errors.append("producer/consumer exact message-ID sets differ or are empty")
        if joined_native_deliveries != {row["delivery"] for row in by_kind["delivery_map"]}:
            errors.append("native delivery maps are missing client message-ID joins")
        if joined_native_links != set(links):
            errors.append("native links are missing exact client readiness joins")
        if client_connections["producer"] and client_connections["consumer"]:
            producer_accepts = [connections[key]["seq"] for key in client_connections["producer"]]
            consumer_accepts = [connections[key]["seq"] for key in client_connections["consumer"]]
            consumer_first = max(consumer_accepts) < min(producer_accepts)
        else:
            consumer_first = False
        result["consumer_before_producer_accept"] = consumer_first
        if not consumer_first:
            errors.append("actual server consumer-before-producer accept order not proven")
        send_starts = defaultdict(list)
        for row in by_kind["socket_send_start"]:
            send_starts[row["connection"]].append(row)
        send_seqs = {connection: [row["seq"] for row in starts] for connection, starts in send_starts.items()}
        send_returns = {(row["connection"], row["value1"]): row for row in by_kind["socket_send_return"]}
        message_stages = []
        for identity in sorted(set(clients["producer"]) & set(clients["consumer"])):
            pub_map = clients["producer"][identity]["map"]
            consumer_map = clients["consumer"][identity]["map"]
            pub_events = events_by_delivery[pub_map["delivery"]]
            consume_events = events_by_delivery[consumer_map["delivery"]]
            claim_events = events_by_operation[consumer_map["operation"]]
            needed_pub = ("publish_start", "publish_return", "accepted_queued")
            needed_consume = ("transfer_queued", "settle_start", "settle_return")
            if any(key not in pub_events for key in needed_pub) or any(key not in consume_events for key in needed_consume) or any(key not in claim_events for key in ("claim_start", "claim_return")):
                errors.append("exact message path has missing native stage spans")
                continue
            stage_rows = {key: pub_events[key] for key in needed_pub}
            stage_rows.update({key: consume_events[key] for key in needed_consume})
            stage_rows.update({key: claim_events[key] for key in ("claim_start", "claim_return")})
            if claim_events["claim_return"]["result"] != 1 or consumer_map["db_id"] != claim_events["claim_return"]["db_id"] or consumer_map["generation"] != claim_events["claim_return"]["generation"]:
                errors.append("claim result does not match consumer delivery DB ID/generation")
            times = {key: value["t_ns"] for key, value in stage_rows.items()}
            ordered = [times[key] for key in (*needed_pub, "claim_start", "claim_return", *needed_consume)]
            if ordered != sorted(ordered):
                errors.append("native message stages occur out of order")
            elapsed = {
                "publish_db_ns": times["publish_return"] - times["publish_start"],
                "publish_return_to_accepted_queued_ns": times["accepted_queued"] - times["publish_return"],
                "accepted_queued_to_claim_start_ns": times["claim_start"] - times["accepted_queued"],
                "claim_db_ns": times["claim_return"] - times["claim_start"],
                "claim_return_to_transfer_queued_ns": times["transfer_queued"] - times["claim_return"],
                "settle_db_ns": times["settle_return"] - times["settle_start"],
            }
            following_sends = {}
            for queued, mapping in (("accepted_queued", pub_map), ("transfer_queued", consumer_map)):
                connection = mapping["connection"]
                position = bisect_right(send_seqs.get(connection, []), stage_rows[queued]["seq"])
                following = send_starts[connection][position] if position < len(send_starts[connection]) else None
                if following is None:
                    errors.append("queued message lacks subsequent owned socket-send observation")
                else:
                    returned = send_returns[(connection, following["value1"])]
                    following_sends[queued] = {"send_sequence": following["value1"], "requested_bytes": following["value2"],
                                               "t_ns": following["t_ns"], "return_t_ns": returned["t_ns"],
                                               "return_bytes": returned["result"], "errno": returned["errno"],
                                               "tcp_nodelay": following["tcp_nodelay"]}
                    elapsed[queued + "_to_following_owned_send_ns"] = following["t_ns"] - times[queued]
            message_stages.append({"id": identity, "producer_connection": pub_map["connection"],
                                   "consumer_connection": consumer_map["connection"],
                                   "producer_link": pub_map["link"], "consumer_link": consumer_map["link"],
                                   "producer_delivery": pub_map["delivery"], "consumer_delivery": consumer_map["delivery"],
                                   "producer_tag_hex": pub_map["data_hex"], "consumer_tag_hex": consumer_map["data_hex"],
                                   "db_id": consumer_map["db_id"], "generation": consumer_map["generation"],
                                   "server_times_ns": times, "server_elapsed_ns": elapsed,
                                   "following_owned_sends": following_sends})
        result["message_stages"] = message_stages
        result["endpoint_observations"] = endpoint_observations
        result["client_identity_join_required"] = False
        result["send_attribution"] = "first following owned socket send; TLS bytes are NOT assigned to individual messages"
        result["errors"] = sorted(set(errors))
        result["complete"] = not errors
        result["status"] = "complete_diagnostic_trace" if not errors else "insufficient"
        return result
    except (KeyError, TypeError, ValueError, OSError, IndexError) as error:
        return {"complete": False, "status": "insufficient", "errors": [f"invalid/incomplete native or client evidence: {error}"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-root", type=Path)
    mode.add_argument("--validate-trace", type=Path)
    parser.add_argument("--client-directory", type=Path)
    args = parser.parse_args()
    if args.validate_trace:
        result = validate_trace(args.validate_trace, args.client_directory)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["complete"] else 2)
    print(json.dumps(materialize(args.repository, args.output_root), indent=2))


if __name__ == "__main__":
    main()
