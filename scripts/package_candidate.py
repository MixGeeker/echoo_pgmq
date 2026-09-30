#!/usr/bin/env python3
"""Create a checksummed candidate archive. Does not publish a release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-config", required=True)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--proton-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    version = subprocess.check_output([args.pg_config, "--version"], text=True).strip()
    major = version.split()[1].split(".")[0]
    suffix = ".dll" if os.name == "nt" else ".so"
    candidates = list(args.build_dir.rglob("echoo_pgmq" + suffix))
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one built extension, found {candidates}")
    artifact = f"echoo-pgmq-0.1.0-candidate-pg{major}-{platform.system().lower()}-{platform.machine().lower()}"
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="echoo-package-") as temp:
        stage = Path(temp) / artifact
        (stage / "lib").mkdir(parents=True)
        (stage / "share" / "extension").mkdir(parents=True)
        shutil.copy2(candidates[0], stage / "lib" / candidates[0].name)
        for path in [ROOT / "echoo_pgmq.control", *sorted((ROOT / "sql").glob("*.sql"))]:
            shutil.copy2(path, stage / "share" / "extension" / path.name)
        runtime = stage / ("runtime-bin" if os.name == "nt" else "lib")
        runtime.mkdir(exist_ok=True)
        dep_files = list((args.proton_root / "bin").glob("*qpid*.dll")) if os.name == "nt" else [
            p for folder in ("lib", "lib64") for p in (args.proton_root / folder).glob("libqpid-proton*.so*")]
        if not dep_files:
            raise SystemExit("Proton runtime library missing; refusing incomplete candidate")
        for path in dep_files:
            if path.is_file():
                shutil.copy2(path, runtime / path.name, follow_symlinks=True)
        for filename in ("README.md", "SECURITY.md"):
            shutil.copy2(ROOT / filename, stage / filename)
        shutil.copytree(ROOT / "docs", stage / "docs")
        (stage / "third-party").mkdir()
        licenses = list(args.proton_root.rglob("LICENSE*")) + list(args.proton_root.rglob("NOTICE*"))
        for path in licenses:
            if path.is_file():
                shutil.copy2(path, stage / "third-party" / ("proton-" + path.name))
        if not any((stage / "third-party").iterdir()):
            raise SystemExit("Proton license/NOTICE absent; install or copy upstream notices before packaging")
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        except subprocess.CalledProcessError:
            commit = "uncommitted"
        files = {str(p.relative_to(stage)).replace(os.sep, "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"status": "candidate-not-production-release", "version": "0.1.0", "commit": commit,
                    "postgres_build": version, "os": platform.platform(), "architecture": platform.machine(),
                    "proton": "0.40.0", "project_license": "pending-owner-decision",
                    "windows11_validation": "not-established-by-server-ci",
                    "files_sha256": files}
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (stage / "INSTALL-CANDIDATE.txt").write_text(
            "候选构建，禁止直接覆盖生产安装。按 docs/quickstart.md 与 docs/admin.md 验证并安装。\n"
            "lib/ 扩展复制到相同 PostgreSQL 主版本的 pkglibdir；share/extension/ 复制到 sharedir/extension。\n"
            "Windows runtime-bin/ 中 Proton DLL 需要由 postgres.exe 找到；先核对依赖、ABI、路径与 ACL。\n"
            "OpenSSL 动态运行库由受信任的系统/PG 发行版提供，包不覆盖现有 OpenSSL DLL。\n"
            "本包没有通过签名发布，也不代表 Windows 11、物理断电、生产 ERP 集成或吞吐指标已经验收。\n",
            encoding="utf-8")
        archive = args.output / (artifact + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    output.write(path, Path(artifact) / path.relative_to(stage))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(archive)


if __name__ == "__main__":
    main()
