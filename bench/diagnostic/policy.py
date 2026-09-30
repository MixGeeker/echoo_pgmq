"""Predeclared one-shot schedule, PG gates and bounded state machine (no I/O)."""
import random

SEED = 20260930
CONTROLLER_SECONDS = 600
WORK_SECONDS = 550  # final 50 seconds reserved for normal stop and evidence
GATE_SECONDS = 20
CELL_SECONDS = 30
WARMUP_SECONDS = 5
TARGET_RATE = 200
MIN_RATE = 190


def schedule(seed=SEED):
    # Uniform choice from all six schedules that contain both orders. No
    # rejection sampling, post-result reshuffle, retries or adaptive ordering.
    choices = (("AB", "AB", "BA"), ("AB", "BA", "AB"), ("BA", "AB", "AB"),
               ("BA", "BA", "AB"), ("BA", "AB", "BA"), ("AB", "BA", "BA"))
    orders = random.Random(seed).choice(choices)
    units = [{"label": "G0", "kind": "gate", "arm": "A", "pair": None}]
    for pair, order in enumerate(orders, 1):
        units.extend({"label": f"P{pair}-{arm}", "kind": "cell", "arm": arm, "pair": pair} for arm in order)
        units.append({"label": f"G{pair}", "kind": "gate", "arm": "A", "pair": pair})
    return {"seed": seed, "algorithm": "random.Random(seed).choice(six mixed schedules); fixed before load",
            "orders": orders, "units": units, "timed_load_seconds": 4*25+6*35}


def distribution(values):
    values = sorted(values)
    return {"count": len(values), "p99_ms": values[int((len(values)-1)*.99)] if values else None}


def gate_result(rows, start_ns, errors=(), duration=GATE_SECONDS):
    if duration != 20:
        raise ValueError("Production gates are exactly twenty measured seconds")
    stop = start_ns + 20_000_000_000
    cohort = [r for r in rows if start_ns <= r["start_ns"] < stop]
    rates = []
    for begin, end in ((start_ns, stop), (start_ns, start_ns+10_000_000_000), (start_ns+10_000_000_000, stop)):
        span = (end-begin)/1e9
        rates.append({"start_tps": sum(begin <= r["start_ns"] < end for r in rows)/span,
                      "completed_tps": sum(begin <= r["complete_ns"] < end for r in rows)/span})
    latencies = [r["latency_ms"] for r in cohort]
    ok = not errors and all(min(r.values()) >= MIN_RATE for r in rates)
    return {"passed": ok, "errors": list(errors), "windows": rates,
            "tps": rates[0]["completed_tps"], **distribution(latencies),
            "above_20ms_fraction": sum(x>20 for x in latencies)/len(latencies) if latencies else None,
            "excess_over_5ms_seconds": sum(max(x-5, 0) for x in latencies)/1000}


def drift_result(previous, current):
    if not previous["passed"] or not current["passed"]:
        return {"passed": False, "reason": "one adjacent gate failed"}
    tps = abs(current["tps"]-previous["tps"])/previous["tps"]
    p99 = abs(current["p99_ms"]-previous["p99_ms"])
    limit = max(1., previous["p99_ms"]*.5)
    return {"passed": tps <= .05 and p99 <= limit, "throughput_relative_change": tps,
            "p99_absolute_change_ms": p99, "p99_allowed_change_ms": limit}


def admit_unit(unit, elapsed):
    # Includes setup/readiness, bounded drain/client exit and normal cleanup.
    required = WARMUP_SECONDS + (GATE_SECONDS if unit["kind"] == "gate" else CELL_SECONDS) + 25
    return {"admitted": elapsed + required <= WORK_SECONDS,
            "required_remaining_seconds": required, "work_remaining_seconds": WORK_SECONDS-elapsed}


class State:
    def __init__(self, plan):
        self.plan, self.results = plan, []
        self.status, self.reason = "running", None
        self.pairs = {str(p): {"status": "unrun", "cells": []} for p in (1,2,3)}
        self.last_gate = None

    def stop(self, status, reason):
        self.status, self.reason = status, reason
        for pair in self.pairs.values():
            if pair["status"] == "pending_post_gate":
                pair["status"] = "invalid_missing_post_gate"

    def add(self, unit, result):
        if self.status != "running":
            raise RuntimeError("No units may run after terminal stop")
        expected = self.plan["units"][len(self.results)]
        if unit != expected:
            raise ValueError("Predeclared order cannot change")
        self.results.append({**unit, **result})
        if unit["kind"] == "gate":
            gate = result["gate"]
            drift = drift_result(self.last_gate, gate) if self.last_gate else None
            self.results[-1]["drift"] = drift
            if not gate["passed"] or (drift and not drift["passed"]):
                if unit["pair"]:
                    self.pairs[str(unit["pair"])]["status"] = "invalid_gate_or_drift"
                self.stop("environment_insufficient", f"{unit['label']} failed PG stability or drift gate")
                return
            if unit["pair"]:
                pair = self.pairs[str(unit["pair"])]
                pair["status"] = "valid_fixed_load_pair" if all(c["fixed_load_eligible"] for c in pair["cells"]) else "diagnostic_only_under_load"
            self.last_gate = gate
        else:
            pair = self.pairs[str(unit["pair"])]
            pair["cells"].append({"label":unit["label"], "fixed_load_eligible": result.get("fixed_load_eligible",False)})
            pair["status"] = "pending_post_gate"
            if result.get("status") != "passed" or not result.get("trace_complete"):
                self.stop("diagnostic_insufficient", f"{unit['label']} measurement or trace incomplete")
        if self.status == "running" and len(self.results) == len(self.plan["units"]):
            self.status = "completed" if all(p["status"] == "valid_fixed_load_pair" for p in self.pairs.values()) else "completed_diagnostic_only"

    def report(self):
        return {"terminal_status": self.status, "reason": self.reason,
                "recorded_units": self.results, "pairs": self.pairs,
                "unrun_units": self.plan["units"][len(self.results):], "automatic_retries": 0}
