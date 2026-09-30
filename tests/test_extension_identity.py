"""SQL checkout and exact installed-body identity; no PostgreSQL required."""
import hashlib
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from scripts.extension_identity import (
    ENQUEUE, candidate_version, installed_identity, source_enqueue_body,
)

ROOT = Path(__file__).resolve().parents[1]


def test_sql_checkout_uses_lf_with_autocrlf(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "core.autocrlf=true", "-c", "core.eol=crlf",
             "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "--quiet")
    (tmp_path / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    # Exercise the actual versioned install and upgrade files, including their
    # nested paths. The text control proves CRLF conversion is active in Git.
    sources = {path.relative_to(ROOT): path.read_bytes()
               for path in sorted((ROOT / "sql").glob("*.sql"))}
    assert sources
    for relative, data in sources.items():
        assert b"\n" in data and b"\r\n" not in data, relative
        destination = tmp_path / relative
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(data)
    (tmp_path / "control.txt").write_bytes(b"first\nsecond\n")
    git("add", ".")
    for relative in [*sources, Path("control.txt")]:
        (tmp_path / relative).unlink()
    git("checkout-index", "--all", "--force")
    assert (tmp_path / "control.txt").read_bytes() == b"first\r\nsecond\r\n"
    for relative, data in sources.items():
        assert (tmp_path / relative).read_bytes() == data
        assert git("show", f":{relative.as_posix()}").stdout == data


@pytest.mark.parametrize("migration", [False, True])
def test_source_body_preserves_exact_utf8_text(tmp_path, migration):
    body = "\nBEGIN\n\tRETURN 7; -- caf\u00e9\nEND\n "
    name = f"echoo_pgmq--{'0.1.1--' if migration else ''}0.1.2.sql"
    (tmp_path / "sql").mkdir()
    statement = ("CREATE OR REPLACE FUNCTION echoo_pgmq._enqueue() "
                 f"RETURNS bigint LANGUAGE plpgsql AS $${body}$$;\n")
    (tmp_path / "sql" / name).write_bytes(statement.encode("utf-8"))
    assert source_enqueue_body("0.1.2", tmp_path, migration=migration) == body


@pytest.mark.parametrize("migration", [False, True])
def test_source_body_rejects_crlf(tmp_path, migration):
    name = f"echoo_pgmq--{'0.1.1--' if migration else ''}0.1.2.sql"
    (tmp_path / "sql").mkdir()
    source = (ROOT / "sql" / name).read_bytes()
    assert b"\r\n" not in source and b"\n" in source
    (tmp_path / "sql" / name).write_bytes(source.replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="contains CRLF; expected canonical LF"):
        source_enqueue_body("0.1.2", tmp_path, migration=migration)


def connection(body, version):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [(version,), (body,)]
    return conn


def test_installed_identity_matches_exact_source():
    version = candidate_version()
    body = source_enqueue_body(version)
    assert installed_identity(connection(body, version)) == {
        "extversion": version,
        "function": ENQUEUE,
        "enqueue_prosrc_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "source_sql_sha256": hashlib.sha256(
            (ROOT / "sql" / f"echoo_pgmq--{version}.sql").read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("mutation", ["crlf", "statement", "trailing-space"])
def test_installed_body_mutation_fails_with_diagnostics(mutation):
    version = candidate_version()
    expected = source_enqueue_body(version)
    if mutation == "crlf":
        actual = expected.replace("\n", "\r\n")
    elif mutation == "statement":
        actual = expected.replace("BEGIN", "BEGIN\n    PERFORM 1;", 1)
    else:
        actual = expected + " "
    assert actual != expected
    with pytest.raises(AssertionError, match="body differs from source version") as failure:
        installed_identity(connection(actual, version))
    message = str(failure.value)
    for label, body in (("actual", actual), ("expected", expected)):
        data = body.encode("utf-8")
        crlf = data.count(b"\r\n")
        assert (f"{label}: sha256={hashlib.sha256(data).hexdigest()}, "
                f"utf8_bytes={len(data)}, crlf={crlf}") in message
        assert body not in message
