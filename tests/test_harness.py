"""Harness invocation contracts; these tests do not run PostgreSQL."""
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import run_integration as harness
finally:
    sys.path.pop(0)


def response(monkeypatch, output, status=0):
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, status, stdout=output)
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    return calls


def invoke():
    return harness.run_sql_script("C:/PostgreSQL 18/bin/psql.exe", "host=127.0.0.1 dbname=owned_test user=fixture",
                                  Path("tests/sql/core.sql"), {"TEST_SCOPE": "ordinary"},
                                  required_marker="core SQL tests passed")


def test_sql_script_uses_explicit_options_and_dsn(monkeypatch):
    calls = response(monkeypatch, "BEGIN\nROLLBACK\ncore SQL tests passed\n")
    assert "core SQL tests passed" in invoke()
    command, options = calls[0]
    assert command == ["C:/PostgreSQL 18/bin/psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "--dbname",
                       "host=127.0.0.1 dbname=owned_test user=fixture", "--file", str(Path("tests/sql/core.sql"))]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.STDOUT
    assert options["env"] == {"TEST_SCOPE": "ordinary"}


def test_sql_script_rejects_ignored_argument_warning(monkeypatch):
    response(monkeypatch, 'psql: warning: extra command-line argument "-f" ignored\ncore SQL tests passed\n')
    with pytest.raises(RuntimeError, match="ignored command-line arguments"):
        invoke()


def test_sql_script_requires_completion_marker(monkeypatch):
    response(monkeypatch, "SELECT 1\n")
    with pytest.raises(RuntimeError, match="did not emit its completion marker"):
        invoke()


def test_sql_script_propagates_nonzero_exit(monkeypatch):
    response(monkeypatch, "ERROR: SQL assertion failed\n", status=3)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        invoke()
    assert failure.value.returncode == 3
