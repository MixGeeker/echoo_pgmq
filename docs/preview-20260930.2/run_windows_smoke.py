#!/usr/bin/env python3
"""Ordinary Windows validation of the new preview.2 guide with fixed original450 binary bytes.

Only this newly created root is eligible for normal pg_ctl fast shutdown. No
service, existing installation, failure injection, or release is involved.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
GUIDE = HERE / "guide"
FIXED = json.loads((HERE / "fixed-inputs.json").read_text(encoding="utf-8"))
MAGIC = "echoo-isolated-pg18-preview-450681d-v2"
PREVIEW_STAGES = ("install", "init", "start", "sql-demo", "amqp-demo", "policy-check", "stop")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_sql_bridge_record(record):
    require(record.get("operation") == "sql-binary" and record.get("client") == "rhea"
            and record.get("version") == "3.0.5" and record.get("received") == 1
            and record.get("accepted") == 1 and record.get("remote_settlement_confirmed") is True
            and record.get("metadata_verified") is True
            and record.get("sql_binary_envelope_verified") is True
            and record.get("binary_body_bytes") == 12 and "wire_base64" not in record,
            "Missing small SQL enqueue_binary to AMQP SECOND settlement evidence")


def verify_policy(record):
    require(record.get("preview_label") == "preview-20260930.2", "Unexpected preview policy version")
    for name in ("max_encoded_message_bytes", "worker_max_message_bytes", "global_max_message_bytes"):
        require(record.get(name) == 65536, "Worker/SQL encoded message limits are inconsistent: " + name)
    for name in ("global_message_count", "global_total_bytes", "retained_message_rows"):
        require(record.get(name) == 0, "Demo retained data or counters remain: " + name)
    queues = record.get("queues", [])
    require(len(queues) == 5 and len({q.get("name") for q in queues}) == 5,
            "Expected five distinct completed demo queues")
    for queue in queues:
        require(queue.get("queue_max_message_bytes") == 65536
                and queue.get("effective_sql_max_message_bytes") == 65536,
                "Demo queue SQL admission differs from worker policy")
        for name in ("queue_message_count", "queue_total_bytes", "retained_message_rows"):
            require(queue.get(name) == 0, "Demo queue was not drained: " + name)


class Smoke:
    def __init__(self):
        temp = Path(os.environ["RUNNER_TEMP"]).resolve()
        # mkdir (not TemporaryDirectory) retains normal inherited Windows ACLs,
        # which native PostgreSQL's restricted-token children need to read.
        self.root = temp / ("echoo-preview-20260930.2-" + uuid.uuid4().hex)
        self.downloads = temp / ("echoo-preview-download-" + uuid.uuid4().hex)
        self.captures = temp / ("echoo-preview-capture-" + uuid.uuid4().hex)
        self.captures.mkdir(mode=0o700)
        self.evidence = temp / "echoo-preview-evidence"
        self.evidence.mkdir(exist_ok=False)
        self.logs = []
        self.owned = False
        self.init_attempted = False
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONUTF8"] = "1"
        self.result = {
            "schema_version": 1,
            "scope": "ordinary isolated install and SQL/AMQP interop only",
            "status": "running",
            "fixed_inputs": FIXED,
            "validation_commit": os.environ.get("GITHUB_SHA"),
            "validation_run_id": os.environ.get("GITHUB_RUN_ID"),
            "validation_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "runner": {"os": platform.system(), "release": platform.release(),
                       "version": platform.version(), "architecture": platform.machine(),
                       "image_os": os.environ.get("ImageOS"),
                       "image_version": os.environ.get("ImageVersion")},
            "script_sha256": {"run_windows_smoke.py": sha256(Path(__file__)),
                              "fixed-inputs.json": sha256(HERE / "fixed-inputs.json")},
            "archive_sha256": {},
            "stages": [],
            "preview_exit_codes": {name: None for name in PREVIEW_STAGES},
            "checks": {"sql_demo_marker": False, "rhea_scenarios": [],
                       "clean_pg_status_exit_code": None},
            "qualification": "not performed; security/parser/fuzz/new fault work paused",
        }

    def run(self, stage, argv, *, expected=0, download=None, github=False):
        args = [str(arg) for arg in argv]
        started = time.monotonic()
        record = {"name": stage, "argv": args, "exit_code": None}
        self.result["stages"].append(record)
        env = os.environ.copy() if github else self.env
        self.logs.append(f"=== {stage} (started) ===\n")
        self.save()
        print(f"Running {stage}", flush=True)
        capture = self.captures / f"{len(self.result['stages']):02d}-{stage}.log"
        # pg_ctl on Windows launches a long-lived CMD process that may retain
        # inherited output handles. Regular files let run() wait for the direct
        # child without waiting for every descendant to close a PIPE writer.
        # Raw captures stay private and outside the four-file upload allowlist.
        with capture.open("xb") as log:
            if download is None:
                process = subprocess.run(args, env=env, stdout=log,
                                         stderr=subprocess.STDOUT)
            else:
                with download.open("xb") as stream:
                    process = subprocess.run(args, env=env, stdout=stream, stderr=log)
        output = capture.read_bytes().decode("utf-8", errors="replace")
        record.update(exit_code=process.returncode,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        if stage in PREVIEW_STAGES:
            self.result["preview_exit_codes"][stage] = process.returncode
        self.logs.append(f"=== {stage} (exit {process.returncode}) ===\n{output}\n")
        self.save()
        print(f"{stage}: exit {process.returncode}", flush=True)
        require(expected is None or process.returncode == expected,
                f"{stage}: expected exit {expected}, got {process.returncode}")
        return process.returncode, output

    def preview(self, action, *extra):
        return self.run(action, [sys.executable, GUIDE / "preview.py", action,
                                 "--root", self.root, *extra])

    def execute(self):
        require(os.name == "nt", "This entry point requires native Windows")
        require(sys.version_info[:2] == (3, 12), "Use the pinned Python 3.12 runtime")
        require(os.environ.get("GITHUB_REPOSITORY") == FIXED["artifact"]["repository"],
                "The fixed artifact must be downloaded from its original repository")
        openssl_value = self.env.get("ECHOO_TEST_OPENSSL", "")
        openssl = Path(openssl_value)
        print(f"ECHOO_TEST_OPENSSL resolved value: {openssl_value!r}", flush=True)
        require(openssl.is_absolute() and openssl.is_file(),
                f"ECHOO_TEST_OPENSSL must name the existing runner OpenSSL executable; received {openssl_value!r}")
        for relative, digest in FIXED["guide_sha256"].items():
            actual = sha256(GUIDE / relative)
            self.result["script_sha256"]["guide/" + relative] = actual
            require(actual == digest, f"Delivered guide bytes changed: {relative}")
        self.run("python-version", [sys.executable, "--version"])
        node_path = shutil.which("node")
        require(node_path is not None, "Node.js 24 is required")
        node_path = Path(node_path).resolve()
        npm_cli = node_path.parent / "node_modules/npm/bin/npm-cli.js"
        require(npm_cli.is_file(), "Expected npm-cli.js from the same Node.js installation")
        self.result["tool_paths"] = {"node": str(node_path), "npm_cli": str(npm_cli),
                                     "openssl": str(openssl)}
        _, node = self.run("node-version", [node_path, "--version"])
        require(node.strip().startswith("v24."), "Use Node.js 24")
        self.run("openssl-version", [openssl, "version", "-a"])
        self.downloads.mkdir()
        artifact = FIXED["artifact"]
        outer = self.downloads / "original-github-artifact.zip"
        endpoint = f"repos/{artifact['repository']}/actions/artifacts/{artifact['artifact_id']}/zip"
        # gh api writes the API response bytes directly to stdout. Python owns
        # the binary file handle, avoiding PowerShell text redirection entirely.
        self.run("download-original-artifact", ["gh", "api", endpoint],
                 download=outer, github=True)
        self.result["archive_sha256"]["outer"] = sha256(outer)
        require(sha256(outer) == artifact["outer_sha256"], "Outer artifact SHA256 mismatch")
        inner = self.downloads / artifact["inner_name"]
        with zipfile.ZipFile(outer) as archive:
            for name in (inner.name, inner.name + ".sha256"):
                members = [info for info in archive.infolist() if info.filename == name]
                require(len(members) == 1, f"Expected one original artifact member: {name}")
                (self.downloads / name).write_bytes(archive.read(members[0]))
        actual = sha256(inner)
        self.result["archive_sha256"]["inner"] = actual
        require(actual == artifact["inner_sha256"], "Inner candidate SHA256 mismatch")
        checksum = inner.with_name(inner.name + ".sha256").read_text(encoding="utf-8").split()
        require(checksum == [actual, inner.name], "Original checksum sidecar mismatch")
        sys.path.insert(0, str(GUIDE / "scripts"))
        from install_candidate import verified_members
        from fetch_dependency import DEPENDENCIES
        manifest, _ = verified_members(inner)
        require(manifest["commit"] == FIXED["candidate_checkout"]
                and manifest["version"] == "0.1.0" and manifest["system"] == "Windows"
                and manifest["postgres_build"].split()[1].split(".")[0] == "18",
                "Fixed candidate manifest identity mismatch")
        self.result["candidate_manifest"] = manifest
        dep = DEPENDENCIES[FIXED["postgres"]["dependency"]]
        require(dep["url"] == FIXED["postgres"]["url"]
                and dep["digest"] == FIXED["postgres"]["sha256"],
                "Official PostgreSQL dependency pin changed")
        self.root.mkdir()
        self.owned = True
        _, fetched = self.run("fetch-postgres", [sys.executable, GUIDE / "scripts/fetch_dependency.py",
                                                FIXED["postgres"]["dependency"], self.root / "pg"])
        require(f"Verified postgres-windows-18: sha256 {FIXED['postgres']['sha256']}" in fetched,
                "Missing upstream PostgreSQL archive verification evidence")
        self.result["archive_sha256"]["postgres_upstream"] = FIXED["postgres"]["sha256"]
        require({item.name for item in self.root.iterdir()} == {"pg"},
                "Fresh preview root must contain only the downloaded pg prefix")
        _, pg_version = self.run("postgres-version", [self.root / "pg/bin/pg_config.exe", "--version"])
        require(pg_version.strip() == "PostgreSQL " + FIXED["postgres"]["version"],
                "Expected the fixed PostgreSQL 18.6 runtime")
        self.run("npm-ci", [node_path, npm_cli, "ci", "--prefix", GUIDE / "interop", "--ignore-scripts",
                            "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"])
        self.run("verify-dependencies", [node_path, GUIDE / "interop/verify_dependencies.js"])
        self.preview("install", "--archive", inner, "--disposable-installation")
        self.init_attempted = True
        self.preview("init")
        self.preview("start")
        _, sql = self.preview("sql-demo")
        self.result["checks"]["sql_demo_marker"] = "SQL_ENQUEUE_READ_ACK_OK" in sql
        require(self.result["checks"]["sql_demo_marker"], "SQL demo completion marker missing")
        _, amqp = self.preview("amqp-demo")
        files = list(self.root.glob("demo-results-*.json"))
        require(len(files) == 1, "Expected one completed ordinary AMQP result")
        records = json.loads(files[0].read_text(encoding="utf-8"))
        require(len(records) == 7, "Expected three publish/consume result pairs and SQL-to-AMQP result")
        for index, operation in enumerate(("roundtrip", "release", "reconnect")):
            published, consumed = records[index * 2:index * 2 + 2]
            require(published["operation"] == "publish" and published["accepted"] == 1
                    and published["published"] == 1, f"Missing publish Accepted: {operation}")
            require(consumed["operation"] == operation and consumed["accepted"] == 1
                    and consumed["remote_settlement_confirmed"] is True
                    and consumed["metadata_verified"] is True
                    and consumed["received"] == (1 if operation == "roundtrip" else 2),
                    f"Missing rhea success evidence: {operation}")
            require(f"{operation}: Accepted, exact stored bytes, metadata verified, manual ACK settled, queue empty" in amqp,
                    f"Missing wrapper bytes/empty-queue evidence: {operation}")
            require(all(row["client"] == "rhea" and row["version"] == "3.0.5"
                        and "wire_base64" not in row for row in (published, consumed)),
                    "Unexpected client or unfiltered message bytes")
            self.result["checks"]["rhea_scenarios"].append(operation)
        verify_sql_bridge_record(records[6])
        self.result["checks"]["sql_binary_bridge"] = True
        (self.evidence / "demo-results.json").write_text(
            json.dumps(records, indent=2) + "\n", encoding="utf-8")
        self.preview("policy-check")
        policy_files = list(self.root.glob("policy-results-*.json"))
        require(len(policy_files) == 1, "Expected one explicit policy evidence record")
        policy = json.loads(policy_files[0].read_text(encoding="utf-8"))
        verify_policy(policy)
        self.result["checks"]["actual_message_policy"] = policy
        (self.evidence / "policy-results.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.preview("stop")
        status, _ = self.run("final-status", [self.root / "pg/bin/pg_ctl.exe", "-D",
                                            self.root / "data", "status"], expected=3)
        self.result["checks"]["clean_pg_status_exit_code"] = status
        require(not (self.root / "data/postmaster.pid").exists(), "Postmaster PID remains after stop")
        self.result["status"] = "passed"

    def cleanup(self):
        # init itself temporarily starts PostgreSQL. Even if that command fails,
        # only a marker belonging to this newly created root permits inspection
        # and (if actually running) a normal fast stop of this specific cluster.
        if not self.owned or not self.init_attempted:
            return
        marker = json.loads((self.root / "preview-state.json").read_text(encoding="utf-8"))
        require(marker.get("magic") == MAGIC and marker.get("root") == str(self.root),
                "Cleanup refused: preview ownership marker mismatch")
        if not (self.root / "data/PG_VERSION").is_file():
            return
        pg_ctl = self.root / "pg/bin/pg_ctl.exe"
        status, _ = self.run("cleanup-status", [pg_ctl, "-D", self.root / "data", "status"], expected=None)
        require(status in (0, 3), f"Cleanup could not determine cluster status: {status}")
        if status == 0:
            self.run("cleanup-normal-stop", [pg_ctl, "-D", self.root / "data",
                                             "-w", "-t", "30", "-m", "fast", "stop"])
            status, _ = self.run("cleanup-final-status", [pg_ctl, "-D", self.root / "data", "status"], expected=3)
        self.result["checks"]["clean_pg_status_exit_code"] = status

    def save(self):
        # Raw local cluster/credential files are never placed under evidence.
        secrets = [os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_TOKEN", "")]
        pgpass = self.root / ".pgpass-preview"
        if pgpass.is_file():
            secrets += [line.rsplit(":", 1)[-1] for line in pgpass.read_text(encoding="utf-8").splitlines()]

        def scrub(text):
            for secret in secrets:
                if secret:
                    text = text.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)(PASSWORD\s+)'[^']*'", r"\1'[REDACTED]'", text)

        (self.evidence / "stages.log").write_text(scrub("\n".join(self.logs)), encoding="utf-8")
        postgres = self.root / "postgres.log"
        if postgres.is_file():
            try:
                snapshot = postgres.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                # Windows may deny a read while CMD/PostgreSQL owns the live
                # log. Preserve the previous snapshot and retry at next stage.
                self.result["postgres_log_snapshot_error"] = f"{type(error).__name__}: {error}"
            else:
                (self.evidence / "postgres.log").write_text(scrub(snapshot), encoding="utf-8")
                self.result.pop("postgres_log_snapshot_error", None)
        (self.evidence / "result.json").write_text(
            scrub(json.dumps(self.result, indent=2) + "\n"), encoding="utf-8")


def main():
    smoke = Smoke()
    try:
        smoke.execute()
    except Exception as error:
        smoke.result.update(status="failed", error=f"{type(error).__name__}: {error}")
        print("Smoke failed; see sanitized result.json and stage logs", file=sys.stderr)
    finally:
        if smoke.result["status"] != "passed":
            try:
                smoke.cleanup()
            except Exception as error:
                smoke.result["cleanup_error"] = f"{type(error).__name__}: {error}"
        smoke.save()
    return 0 if smoke.result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
