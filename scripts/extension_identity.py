"""Compare the installed enqueue body with the versioned source being tested."""
import hashlib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ENQUEUE = "echoo_pgmq._enqueue(text,bytea,text,text)"


def candidate_version(root=ROOT):
    match = re.search(r"^default_version\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'$",
                      (root / "echoo_pgmq.control").read_text(encoding="utf-8"), re.M)
    if not match:
        raise ValueError("missing candidate default_version")
    return match[1]


def source_enqueue_body(version, root=ROOT, migration=False):
    name = f"echoo_pgmq--{'0.1.1--' if migration else ''}{version}.sql"
    text = (root / "sql" / name).read_text(encoding="utf-8")
    matches = re.findall(r"CREATE (?:OR REPLACE )?FUNCTION echoo_pgmq\._enqueue\(.*?"
                         r"AS \$\$(.*?)\$\$;", text, re.S)
    if len(matches) != 1:
        raise ValueError(f"expected one _enqueue body in {name}")
    return matches[0]


def installed_identity(conn, version=None):
    expected_version = version or candidate_version()
    actual_version = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq'").fetchone()[0]
    if actual_version != expected_version:
        raise AssertionError(f"installed extension {actual_version}, expected {expected_version}")
    body, = conn.execute("SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure", (ENQUEUE,)).fetchone()
    expected_body = source_enqueue_body(expected_version)
    if body != expected_body:
        raise AssertionError(f"installed {ENQUEUE} body differs from source version {expected_version}")
    return {"extversion": actual_version, "function": ENQUEUE,
            "enqueue_prosrc_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "source_sql_sha256": hashlib.sha256(
                (ROOT / "sql" / f"echoo_pgmq--{expected_version}.sql").read_bytes()).hexdigest()}
