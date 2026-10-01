#!/usr/bin/env python3
"""Verify and install a candidate only into an explicitly disposable PostgreSQL installation.

This CI helper replaces Echoo/Proton files in pg_config's installation, never
OpenSSL or PostgreSQL DLLs. Stop every process using that installation first.
It is deliberately not a production installer or an upgrade mechanism.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import zipfile


def verified_members(archive):
    archive = Path(archive)
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    expected = checksum.read_text(encoding="ascii").split()
    if len(expected) != 2 or expected[1] != archive.name:
        raise ValueError("invalid archive checksum record")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected[0]:
        raise ValueError("archive SHA256 mismatch")
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        names = [item.filename for item in members]
        if not names or len(set(names)) != len(names):
            raise ValueError("empty archive or duplicate member")
        roots = set()
        for item in members:
            path = PurePosixPath(item.filename)
            # ZipInfo keeps the original spelling but normalizes backslashes
            # on Windows. Reject that raw input before trusting the manifest.
            if (item.orig_filename != item.filename or "\\" in item.orig_filename
                    or path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or ":" in item.filename or len(path.parts) < 2
                    or stat.S_ISLNK(item.external_attr >> 16) or item.is_dir()):
                raise ValueError("unsafe archive member")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("archive must have one root directory")
        prefix = roots.pop() + "/"
        manifest = json.loads(source.read(prefix + "MANIFEST.json"))
        declared = manifest["files_sha256"]
        actual = {name[len(prefix):]: hashlib.sha256(source.read(name)).hexdigest()
                  for name in names if name != prefix + "MANIFEST.json"}
        if actual != declared:
            raise ValueError("manifest file list or SHA256 mismatch")
        if manifest.get("status") != "candidate-not-production-release":
            raise ValueError("unexpected candidate status")
        if 'distribution_version' in manifest:
            if manifest.get('version') != manifest['distribution_version'] or manifest.get('native_build_version') != manifest['distribution_version']:
                raise ValueError('distribution/native identity mismatch')
            control = source.read(prefix + 'share/extension/echoo_pgmq.control').decode('utf-8')
            match = re.search(r"default_version\s*=\s*'([^']+)'", control)
            if not match or match.group(1) != manifest.get('sql_default_version') or manifest.get('extensionVersion') != manifest.get('sql_default_version'):
                raise ValueError('SQL default identity mismatch')
            for version in manifest.get('sql_available_versions', []):
                if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) or prefix + f'share/extension/echoo_pgmq--{version}.sql' not in names:
                    raise ValueError('SQL version file missing')
            if manifest.get('sql_default_version') not in manifest.get('sql_available_versions', []):
                raise ValueError('SQL default not available')
        return manifest, {name[len(prefix):]: source.read(name) for name in names}


def install(archive, pg_config):
    manifest, members = verified_members(archive)
    def pg_value(option):
        return subprocess.check_output([str(pg_config), option], text=True).strip()
    version = pg_value("--version")
    if version.split()[1].split(".")[0] != manifest["postgres_build"].split()[1].split(".")[0]:
        raise ValueError("candidate and target PostgreSQL major versions differ")
    if platform.system() != manifest["system"]:
        raise ValueError("candidate and target operating systems differ")
    if platform.machine().lower() != manifest["architecture"].lower():
        raise ValueError("candidate and target architectures differ")
    suffix = ".dll" if os.name == "nt" else ".so"
    if "lib/echoo_pgmq" + suffix not in members:
        raise ValueError("candidate native module does not match this platform")
    library = Path(pg_value("--pkglibdir"))
    shared = Path(pg_value("--sharedir")) / "extension"
    runtime = Path(pg_value("--bindir")) if os.name == "nt" else library
    copies = []
    for name, content in members.items():
        parts = PurePosixPath(name).parts
        if parts[0] == "lib" and len(parts) == 2:
            filename = parts[1]
            if filename != "echoo_pgmq" + suffix and not (os.name != "nt" and filename.startswith("libqpid-proton") and ".so" in filename):
                raise ValueError("unexpected native library")
            copies.append((library / filename, content))
        elif parts[:2] == ("share", "extension") and len(parts) == 3:
            if not (parts[2] == "echoo_pgmq.control" or (parts[2].startswith("echoo_pgmq--") and parts[2].endswith(".sql"))):
                raise ValueError("unexpected extension SQL/control")
            copies.append((shared / parts[2], content))
        elif parts[0] == "runtime-bin" and len(parts) == 2:
            if os.name != "nt" or "qpid" not in parts[1] or not parts[1].endswith(".dll"):
                raise ValueError("unexpected runtime dependency")
            copies.append((runtime / parts[1], content))
    if not any("qpid" in path.name for path, _ in copies):
        raise ValueError("candidate lacks Proton runtime")
    if not any(path.name == "echoo_pgmq.control" for path, _ in copies):
        raise ValueError("candidate lacks extension control file")
    # Validation completes before replacing anything. Delete the old candidate
    # installation so no omitted SQL or Proton file can mask an incomplete ZIP.
    for folder, pattern in ((library, "echoo_pgmq.*"), (shared, "echoo_pgmq*"),
                            (runtime, "*qpid*.dll" if os.name == "nt" else "libqpid-proton*.so*")):
        for path in folder.glob(pattern):
            if path.is_file() or path.is_symlink():
                path.unlink()
    for path, content in copies:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
            raise ValueError("installed file checksum mismatch")
        print("Installed:", path)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--pg-config", required=True)
    parser.add_argument("--disposable-installation", action="store_true",
                        help="confirm that every cluster using this PostgreSQL installation is stopped and disposable")
    args = parser.parse_args()
    if not args.disposable_installation:
        parser.error("requires --disposable-installation; never use on a production installation")
    manifest = install(args.archive, args.pg_config)
    print("Verified candidate from commit", manifest["commit"])


if __name__ == "__main__":
    main()
