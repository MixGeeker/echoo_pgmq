#!/usr/bin/env python3
"""Fail early with DLL diagnostics for the isolated native Windows test install."""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("postgres", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("native Windows only")
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)
    bindir = args.postgres.resolve() / "bin"
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    failed = []
    # Keep handles alive while testing dependency loading. No extension is loaded
    # into Python; extension execution belongs exclusively to PostgreSQL.
    handles = []
    for name in ("api-ms-win-crt-private-l1-1-0.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll",
                 "libiconv-2.dll", "libintl-9.dll", "icuuc77.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libpq.dll"):
        path = bindir / name
        # PG16/17 can ship a different ICU major. The executable probes below
        # verify the actual dependency closure rather than requiring ICU77.
        if name.startswith("icu") and not path.exists():
            continue
        try:
            handles.append(ctypes.WinDLL(str(path) if path.exists() else name, winmode=0))
            print("DLL loaded:", name, flush=True)
        except OSError as error:
            failed.append((name, str(error)))
            print("DLL diagnostic:", name, error, flush=True)
    for name in ("initdb", "postgres", "psql", "pg_ctl"):
        result = subprocess.run([str(bindir / (name + ".exe")), "--version"], capture_output=True, text=True)
        print(name, "exit", hex(result.returncode), result.stdout, result.stderr, flush=True)
        if result.returncode:
            raise SystemExit("Native PostgreSQL runtime cannot load; see DLL diagnostics above")
    # An optional diagnostic DLL can be absent while the real native executables
    # run successfully, so the executable results remain authoritative.
    # --version returns before initdb's restricted-token re-exec. A real
    # disposable initialization validates that loader path too, without changing
    # token protections, starting a server, or loading the Echoo extension.
    with tempfile.TemporaryDirectory(prefix="echoo-native-prerequisite-", dir=args.postgres.parent) as temp:
        result = subprocess.run([str(bindir / "initdb.exe"), "-D", str(Path(temp) / "data"),
                                 "-U", "echoo_runtime_probe", "-A", "trust", "--no-locale", "--encoding=UTF8"],
                                capture_output=True, text=True)
        print("initdb initialization exit", hex(result.returncode), result.stdout, result.stderr, flush=True)
        if result.returncode:
            raise SystemExit("Native PostgreSQL restricted-token initialization failed")
    print("Native PostgreSQL runtime and initialization probes passed", flush=True)


if __name__ == "__main__":
    main()
