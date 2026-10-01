#!/usr/bin/env python3
"""User-run, loopback-only PG18 preview. Never point this at an ERP installation.

This wrapper performs ordinary operations only. It never registers a service,
changes a firewall, runs qualification, kills a process, or removes a cluster.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))
from install_candidate import install, verified_members
from make_test_certs import generate

MAGIC = "echoo-isolated-pg18-preview-450681d-v1"
EXPECTED_ARCHIVES = {
    "Windows": "a8107211653c966dca2792726a84e4013bd7c1eb010e40700f755d3d425ff164",
    "Linux": "e73ba411f3957b8a28bca00e1c9104353f19645160c650dad03b6f8df8083052",
}


def run(argv, *, env=None, input=None, capture=False):
    return subprocess.run([str(x) for x in argv], env=env, input=input,
                          text=True, encoding="utf-8", check=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def private_write(path, text):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def quote(value):
    return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Preview:
    def __init__(self, root):
        require(root.is_absolute(), "--root must be an absolute path")
        self.root = root.resolve()
        require(self.root.name.startswith("echoo-preview-"),
                "The isolated root directory name must start with echoo-preview-")
        require(self.root != Path.home().resolve(), "Do not use your home directory as root")
        self.prefix = self.root / "pg"
        self.data = self.root / "data"
        self.certs = self.root / "certs"
        self.marker = self.root / "preview-state.json"
        self.bindir = self.prefix / "bin"
        self.env = dict(os.environ)
        for name in list(self.env):
            if name.startswith("PG") or name in ("LD_PRELOAD", "NODE_OPTIONS", "NODE_TLS_REJECT_UNAUTHORIZED"):
                self.env.pop(name)
        self.env["PATH"] = str(self.bindir) + os.pathsep + self.env.get("PATH", "")
        self.env["PGPASSFILE"] = str(self.root / ".pgpass-preview")
        self.state = None

    def binary(self, name):
        return self.bindir / (name + (".exe" if os.name == "nt" else ""))

    def check_prefix(self):
        require(self.prefix.resolve() == self.prefix, "pg must not be a symlink or junction")
        require(self.binary("pg_config").is_file(), "Install standalone PG18 into ROOT/pg first")
        version = run([self.binary("pg_config"), "--version"], env=self.env, capture=True).strip()
        require(version.split()[1].split(".")[0] == "18", "Only PG18 is allowed")
        for option in ("--bindir", "--pkglibdir", "--sharedir"):
            folder = Path(run([self.binary("pg_config"), option], env=self.env, capture=True).strip()).resolve()
            require(folder.is_relative_to(self.prefix), f"pg_config {option} escapes the disposable prefix: {folder}")
        for name in ("initdb", "postgres", "psql", "pg_ctl"):
            run([self.binary(name), "--version"], env=self.env)

    def load(self):
        self.state = json.loads(self.marker.read_text(encoding="utf-8"))
        require(self.state.get("magic") == MAGIC and self.state.get("root") == str(self.root),
                "Preview identity/root mismatch; do not move an initialized root")
        require(self.data.resolve() == self.data and self.certs.resolve() == self.certs,
                "data/certs must not be symlinks or junctions")
        self.check_prefix()

    def pg(self, action):
        return run([self.binary("pg_ctl"), "-D", self.data, "-w", "-t", "30",
                    *(["-m", "fast"] if action == "stop" else ["-l", self.root / "postgres.log"]),
                    action], env=self.env)

    def sql(self, text, *, user="echoo_admin", database="echoo_preview", capture=False):
        return run([self.binary("psql"), "-X", "-w", "-v", "ON_ERROR_STOP=1",
                    "-h", "127.0.0.1", "-p", self.state["pg_port"], "-U", user,
                    "-d", database, "-A", "-t"], env=self.env, input=text, capture=capture)

    def install(self, archive, confirmed):
        require(confirmed, "Pass --disposable-installation only for a new dedicated PG prefix")
        require(self.root.is_dir(), "Prepare ROOT/pg before installation")
        require(set(p.name for p in self.root.iterdir()) == {"pg"},
                "Install requires an isolated root containing only pg; existing data/keys are refused")
        if os.name != "nt":
            require(os.geteuid() != 0, "Run as an ordinary user, not root")
        self.check_prefix()
        archive = archive.resolve()
        manifest, _ = verified_members(archive)
        require(hashlib.sha256(archive.read_bytes()).hexdigest() == EXPECTED_ARCHIVES.get(manifest["system"]),
                "Archive is not one of the two fixed PG18 preview artifacts")
        require(manifest["commit"] == "191d9947d181aab14667562513eae54109a7ea54"
                and manifest["version"] == "0.1.0", "Unexpected candidate identity")
        install(archive, self.binary("pg_config"))
        private_write(self.marker, json.dumps({"magic": MAGIC, "root": str(self.root),
                                              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2) + "\n")
        print("Candidate installed into dedicated prefix only. Next: init")

    def init(self, pgport, amqpport):
        self.load()
        require(not self.data.exists() and not self.certs.exists(), "init requires fresh data and certs directories")
        require(not (self.root / ".pgpass-preview").exists(), "Existing credentials found; use a fresh root")
        require(pgport != amqpport and all(1024 <= p <= 65535 for p in (pgport, amqpport)),
                "Use two different unprivileged TCP ports")
        for port in (pgport, amqpport):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        generate(self.certs)
        admin_password, user_password = secrets.token_hex(24), secrets.token_hex(24)
        pwfile = self.root / ".init-password"
        private_write(pwfile, admin_password + "\n")
        private_write(self.root / ".pgpass-preview",
                      f"127.0.0.1:{pgport}:*:echoo_admin:{admin_password}\n"
                      f"127.0.0.1:{pgport}:echoo_preview:echoo_test_user:{user_password}\n")
        try:
            run([self.binary("initdb"), "-D", self.data, "-U", "echoo_admin",
                 "--auth=scram-sha-256", "--pwfile", pwfile, "--encoding=UTF8", "--no-locale"], env=self.env)
        finally:
            pwfile.unlink(missing_ok=True)
        self.state.update(pg_port=pgport, amqp_port=amqpport)
        self.marker.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
        settings = f"\nlisten_addresses='127.0.0.1'\nport={pgport}\nfsync=on\nfull_page_writes=on\nsynchronous_commit=on\n"
        if os.name != "nt":
            settings += "unix_socket_directories=''\n"
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write(settings)
        self.pg("start")
        try:
            self.sql("CREATE DATABASE echoo_preview;", database="postgres")
            self.sql("""
CREATE EXTENSION echoo_pgmq VERSION '0.1.0';
CREATE ROLE echoo_pgmq_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE echoo_test_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %s;
GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_pgmq_worker;
GRANT EXECUTE ON FUNCTION echoo_pgmq.publish(text,bytea,text),
 echoo_pgmq.claim(text,text,uuid,integer), echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
 echoo_pgmq.authorize(text,text,text) TO echoo_pgmq_worker;
""" % quote(user_password))
        finally:
            self.pg("stop")
        settings = {
            "shared_preload_libraries": "echoo_pgmq", "echoo_pgmq.enabled": "on",
            "echoo_pgmq.database": "echoo_preview", "echoo_pgmq.role": "echoo_pgmq_worker",
            "echoo_pgmq.listen_address": "127.0.0.1", "echoo_pgmq.port": amqpport,
            "echoo_pgmq.tls_certificate": self.certs / "server.pem",
            "echoo_pgmq.tls_private_key": self.certs / "server.key",
            "echoo_pgmq.tls_ca_file": self.certs / "ca.pem", "echoo_pgmq.visibility_seconds": 2,
            "echoo_pgmq.poll_interval_ms": 20, "echoo_pgmq.max_message_bytes": 65536,
            "echoo_pgmq.max_connections": 8, "echoo_pgmq.max_links_per_connection": 4,
            "echoo_pgmq.max_inflight_per_link": 4,
        }
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write("\n".join(k + "=" + quote(v) for k, v in settings.items()) + "\n")
        print("Initialized and stopped. Credentials stay local. Next: start")

    def queue(self, label):
        name = "preview/" + label + "_" + uuid.uuid4().hex
        self.sql(f"SELECT echoo_pgmq.create_queue('{name}'); SELECT echoo_pgmq.grant_queue('{name}', 'echoo_test_user');")
        return name

    def empty(self, address):
        deadline = time.monotonic() + 5
        while True:
            result = self.sql("SELECT q.message_count, (SELECT count(*) FROM echoo_pgmq.messages m "
                              "WHERE m.queue_id=q.queue_id) FROM echoo_pgmq.queues q "
                              f"WHERE q.name='{address}';", capture=True).strip()
            if result == "0|0":
                return
            require(time.monotonic() < deadline, "Queue not empty after ACK: " + result)
            time.sleep(0.1)

    def sql_demo(self):
        self.load()
        address, owner = self.queue("sql"), uuid.uuid4()
        self.sql(f"""
CREATE TEMP TABLE preview_orders(id integer PRIMARY KEY, state text);
BEGIN;
INSERT INTO preview_orders VALUES (42, 'created');
SELECT echoo_pgmq.enqueue_binary('{address}', convert_to('{{"order_id":42,"synthetic":true}}','UTF8'), 'demo-order-42');
COMMIT;
SELECT id, generation, body = echoo_pgmq.message_binary(convert_to('{{"order_id":42,"synthetic":true}}','UTF8')) AS body_matches
FROM echoo_pgmq.read('{address}', '{owner}', 30) \\gset receipt_
\\if :receipt_body_matches
SELECT echoo_pgmq.ack('{address}', :receipt_id, :receipt_generation, '{owner}') AS acked \\gset
\\if :acked
\\echo SQL_ENQUEUE_READ_ACK_OK
\\else
\\quit 1
\\endif
\\else
\\quit 1
\\endif
""", user="echoo_test_user")
        self.empty(address)
        print("SQL transaction enqueue/read/receipt ACK complete; queue empty")

    def amqp_demo(self):
        self.load()
        node = shutil.which("node")
        require(node is not None, "Install Node.js 24 and the locked interop npm dependencies")
        require(run([node, "--version"], capture=True).startswith("v24."), "This preview pins the ordinary demo to Node.js 24")
        run([node, HERE / "interop" / "verify_dependencies.js"])
        env = dict(self.env, ECHOO_TEST_CERT_DIR=str(self.certs),
                   ECHOO_TEST_AMQP_URL=f"amqps://localhost:{self.state['amqp_port']}")
        records = []
        for operation in ("roundtrip", "release", "reconnect"):
            address = self.queue(operation)
            def client(action):
                result = json.loads(run([node, HERE / "interop" / "rhea_client.js", action,
                                         address, "manual-second"], env=env, capture=True))
                records.append({k: v for k, v in result.items() if k != "wire_base64"})
                return result
            published = client("publish")
            stored = self.sql("SELECT encode(body,'hex') FROM echoo_pgmq.messages "
                              "JOIN echoo_pgmq.queues USING(queue_id) "
                              f"WHERE name='{address}';", capture=True).strip()
            require(stored == base64.b64decode(published["wire_base64"], validate=True).hex(),
                    "Accepted message wire bytes did not match storage")
            result = client(operation)
            require(result["accepted"] == 1 and result["remote_settlement_confirmed"], "Missing remote ACK settlement")
            require(result["received"] == (1 if operation == "roundtrip" else 2), "Unexpected receive count")
            self.empty(address)
            print(f"{operation}: Accepted, exact stored bytes, metadata verified, manual ACK settled, queue empty")
        evidence = self.root / ("demo-results-" + uuid.uuid4().hex + ".json")
        private_write(evidence, json.dumps(records, indent=2, ensure_ascii=False) + "\n")
        print("Credential-free ordinary demo results:", evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "init", "start", "status", "sql-demo", "amqp-demo", "stop"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--disposable-installation", action="store_true")
    parser.add_argument("--pg-port", type=int, default=55418)
    parser.add_argument("--amqp-port", type=int, default=56718)
    args = parser.parse_args()
    preview = Preview(args.root)
    if args.action == "install":
        require(args.archive is not None, "install needs --archive pointing to the inner ZIP")
        preview.install(args.archive, args.disposable_installation)
    elif args.action == "init":
        preview.init(args.pg_port, args.amqp_port)
    elif args.action in ("start", "stop", "status"):
        preview.load()
        require("pg_port" in preview.state, "Run init first")
        if args.action == "status":
            result = subprocess.run([str(preview.binary("pg_ctl")), "-D", str(preview.data), "status"], env=preview.env)
            require(result.returncode in (0, 3), "Could not determine cluster status")
        else:
            preview.pg(args.action)
    elif args.action == "sql-demo":
        preview.sql_demo()
    elif args.action == "amqp-demo":
        preview.amqp_demo()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print("Preview stopped with error:", error, file=sys.stderr)
        print("Inspect the local postgres.log; keep the directory for diagnosis. No files were automatically removed.", file=sys.stderr)
        sys.exit(1)
