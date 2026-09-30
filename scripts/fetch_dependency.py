#!/usr/bin/env python3
"""Download pinned upstream build inputs and verify bytes before extraction."""
import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

DEPENDENCIES = {
    "proton": {
        "url": "https://archive.apache.org/dist/qpid/proton/0.40.0/qpid-proton-0.40.0.tar.gz",
        "algorithm": "sha512",
        "digest": "3e7fe56ca1423f45f71d81f5e1d6ec5f21c073cc580628e12a8dbd545a86805b7312834e0d1234dde43797633d575ed639f21a96239b217500cc0a824482aae3",
    },
    # Direct official EDB archives; hashes measured from these exact upstream URLs.
    # Refresh through review, not by fetching a checksum from the same URL at runtime.
    "postgres-windows-16": {
        "url": "https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip",
        "algorithm": "sha256",
        "digest": "25e6fcdfb8caec38691bf461125e7564508760666f7b8e5dc6a5f0818f58f81e",
    },
    "postgres-windows-17": {
        "url": "https://get.enterprisedb.com/postgresql/postgresql-17.11-1-windows-x64-binaries.zip",
        "algorithm": "sha256",
        "digest": "6eabdf00d2893713b75db4336a23c3fdf505f056e217ec6e2e95d901750cfea3",
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dependency", choices=DEPENDENCIES)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    spec = DEPENDENCIES[args.dependency]
    destination = args.destination.resolve()
    if destination.exists():
        parser.error("destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="echoo-download-", dir=destination.parent) as temp:
        archive = Path(temp) / "download"
        digest = hashlib.new(spec["algorithm"])
        with urllib.request.urlopen(spec["url"], timeout=120) as response, archive.open("wb") as output:
            while block := response.read(1024 * 1024):
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != spec["digest"]:
            raise SystemExit(f"Integrity verification failed for {args.dependency}")
        print(f"Verified {args.dependency}: {spec['algorithm']} {digest.hexdigest()}")
        extracted = Path(temp) / "extracted"
        extracted.mkdir()
        if spec["url"].endswith(".zip"):
            with zipfile.ZipFile(archive) as source:
                for entry in source.infolist():
                    candidate = (extracted / entry.filename).resolve()
                    if not candidate.is_relative_to(extracted):
                        raise SystemExit("unsafe ZIP member")
                source.extractall(extracted)
        else:
            with tarfile.open(archive) as source:
                source.extractall(extracted, filter="data")
        roots = list(extracted.iterdir())
        if len(roots) == 1 and roots[0].is_dir():
            shutil.move(str(roots[0]), destination)
        else:
            shutil.move(str(extracted), destination)


if __name__ == "__main__":
    main()
