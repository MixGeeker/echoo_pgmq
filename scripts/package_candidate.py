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

from extension_identity import candidate_version

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-config", required=True)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--proton-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    cache = args.build_dir / "CMakeCache.txt"
    if not cache.is_file():
        raise SystemExit("CMake build cache missing; cannot verify production build flags")
    if "ECHOO_ENABLE_TEST_HOOKS:BOOL=ON" in cache.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit("Refusing to package a fault-injection-enabled build")
    extension_version = candidate_version(ROOT)
    version = subprocess.check_output([args.pg_config, "--version"], text=True).strip()
    major = version.split()[1].split(".")[0]
    suffix = ".dll" if os.name == "nt" else ".so"
    candidates = list(args.build_dir.rglob("echoo_pgmq" + suffix))
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one built extension, found {candidates}")
    # Package the installed module, whose CMake INSTALL_RPATH is $ORIGIN, not
    # the build-tree module with a machine-specific absolute BUILD_RPATH.
    pkglibdir = Path(subprocess.check_output([args.pg_config, "--pkglibdir"], text=True).strip())
    installed = pkglibdir / ("echoo_pgmq" + suffix)
    if not installed.is_file():
        raise SystemExit("installed module missing; run cmake --install before packaging")
    if b"echoo_pgmq.test_fail_settle_before_commit" in installed.read_bytes():
        raise SystemExit("Refusing installed module containing fault-injection hook; reinstall normal build")
    artifact = f"echoo-pgmq-{extension_version}-candidate-pg{major}-{platform.system().lower()}-{platform.machine().lower()}"
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="echoo-package-") as temp:
        stage = Path(temp) / artifact
        (stage / "lib").mkdir(parents=True)
        (stage / "share" / "extension").mkdir(parents=True)
        shutil.copy2(installed, stage / "lib" / installed.name)
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
        licenses = list(args.proton_root.rglob("LICENSE*")) + list(args.proton_root.rglob("NOTICE*")) + list(args.proton_root.rglob("ECHOO_WINDOWS_OPENSSL_PATCH.txt"))
        for path in licenses:
            if path.is_file():
                shutil.copy2(path, stage / "third-party" / ("proton-" + path.name))
        if not list((stage / "third-party").glob("proton-LICENSE*")) or not list((stage / "third-party").glob("proton-NOTICE*")):
            raise SystemExit("Proton license/NOTICE absent; install or copy upstream notices before packaging")
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        except subprocess.CalledProcessError:
            commit = "uncommitted"
        (stage / "INSTALL-CANDIDATE.txt").write_text(
            "候选构建，禁止直接覆盖生产安装。按 docs/quickstart.md 与 docs/admin.md 验证并安装。\n"
            "lib/ 扩展复制到相同 PostgreSQL 主版本的 pkglibdir；share/extension/ 复制到 sharedir/extension。\n"
            "Windows runtime-bin/ 中 Proton DLL 需要由 postgres.exe 找到；先核对依赖、ABI、路径与 ACL。\n"
            "OpenSSL 动态运行库由受信任的系统/PG 发行版提供，包不覆盖现有 OpenSSL DLL。\n"
            "完整 qualification 当前状态为 blocked_security_review，普通回归通过不能替代完整验收。\n"
            "本包没有通过签名发布，也不代表 Windows 11、物理断电、生产 ERP 集成或吞吐指标已经验收。\n",
            encoding="utf-8")
        files = {str(p.relative_to(stage)).replace(os.sep, "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"status": "candidate-not-production-release", "version": extension_version, "commit": commit,
                    "qualification_status": "blocked_security_review",
                    "postgres_build": version, "os": platform.platform(), "system": platform.system(), "architecture": platform.machine(),
                    "proton": "0.40.0",
                    "proton_windows_openssl_patch": bool(list(args.proton_root.rglob("ECHOO_WINDOWS_OPENSSL_PATCH.txt"))),
                    "cmake_cache_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
                    "project_license": "pending-owner-decision",
                    "runtime_dependencies": {
                        "bundled": "Apache Qpid Proton 0.40.0",
                        "external": ["matching PostgreSQL major and architecture", "OpenSSL 3.x from trusted PostgreSQL/system distribution", "platform C runtime (Microsoft Visual C++/UCRT on Windows)"],
                        "openssl_bundled": False,
                        "test_python_runtime_required_for_service": False},
                    "windows11_validation": "not-established-by-server-ci",
                    "files_sha256": files}
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
