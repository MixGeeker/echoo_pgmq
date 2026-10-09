"""Archive validation does not require PostgreSQL or invoke native code."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("install_candidate", Path(__file__).resolve().parents[1] / "scripts/install_candidate.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def archive(tmp_path, *, extra=None, digest=None):
    path = tmp_path / "candidate.zip"
    body = b"test module, never executed"
    manifest = {"status": "candidate-not-production-release",
                "files_sha256": {"lib/echoo_pgmq.so": digest or hashlib.sha256(body).hexdigest()}}
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("candidate/lib/echoo_pgmq.so", body)
        output.writestr("candidate/MANIFEST.json", json.dumps(manifest))
        if extra:
            # Force the same raw member bytes on every OS. ZipInfo's Windows
            # constructor would otherwise normalize the tested backslash.
            member = zipfile.ZipInfo("placeholder")
            member.filename = extra
            output.writestr(member, b"unlisted")
    path.with_suffix(".zip.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  candidate.zip\n")
    return path


def test_candidate_manifest_and_archive_checksums(tmp_path):
    manifest, members = installer.verified_members(archive(tmp_path))
    assert manifest["status"] == "candidate-not-production-release"
    assert members["lib/echoo_pgmq.so"] == b"test module, never executed"


def test_candidate_preserves_project_and_dependency_notices(tmp_path, monkeypatch):
    """Exercise the real packager with inert files, without PG or native execution."""
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("package_candidate", repo / "scripts/package_candidate.py")
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    project_license = (repo / "LICENSE").read_bytes()
    # Exact unmodified https://www.apache.org/licenses/LICENSE-2.0.txt (LF).
    assert hashlib.sha256(project_license).hexdigest() == "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    project_notice = (repo / "NOTICE").read_bytes()
    assert b"Copyright 2026 MixGeeker" in project_notice
    source, build, installed, proton = [tmp_path / name for name in ("source", "build", "installed", "proton")]
    for folder in (source / "sql", source / "docs", build, installed, proton / "bin", proton / "lib", proton / "licenses"):
        folder.mkdir(parents=True, exist_ok=True)
    for name, content in {"LICENSE": project_license, "NOTICE": project_notice,
                          "README.md": b"Packaging fixture", "SECURITY.md": b"Fixture only",
                          "echoo_pgmq.control": b"default_version = '0.1.0'"}.items():
        (source / name).write_bytes(content)
    (source / "sql/echoo_pgmq--0.1.0.sql").write_text("-- inert packaging fixture\n")
    (source / "sql/echoo_pgmq--0.1.1.sql").write_text("-- inert optional SQL fixture\n")
    (source / "sql/echoo_pgmq--0.1.2.sql").write_text("-- inert optional SQL fixture\n")
    (source / "package_versions.json").write_bytes((repo / "package_versions.json").read_bytes())
    (source / "CMakeLists.txt").write_text("project(echoo_pgmq VERSION 0.1.2 LANGUAGES C)\n")
    (build / "CMakeCache.txt").write_text("ECHOO_ENABLE_TEST_HOOKS:BOOL=OFF\nCMAKE_PROJECT_VERSION:STATIC=0.1.2\n")
    suffix = ".dll" if os.name == "nt" else ".so"
    for folder in (build, installed):
        (folder / ("echoo_pgmq" + suffix)).write_bytes(b"inert module fixture; never executed")
    runtime = proton / ("bin/qpid-proton.dll" if os.name == "nt" else "lib/libqpid-proton.so")
    runtime.write_bytes(b"inert dependency fixture; never executed")
    upstream = {"LICENSE.txt": b"Synthetic upstream license fixture\n",
                "NOTICE.txt": b"Synthetic upstream attribution fixture\n",
                "ECHOO_WINDOWS_OPENSSL_PATCH.txt": b"Synthetic modification notice fixture\n"}
    for name, content in upstream.items():
        (proton / "licenses" / name).write_bytes(content)

    def checked_output(command, **kwargs):
        if command == ["fixture-pg-config", "--version"]:
            return "PostgreSQL 18.6\n"
        if command == ["fixture-pg-config", "--pkglibdir"]:
            return str(installed) + "\n"
        if command == ["git", "rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        raise AssertionError(f"Unexpected external command: {command}")

    output = tmp_path / "dist"
    monkeypatch.setattr(packager, "ROOT", source)
    monkeypatch.setattr(packager.platform, "platform", lambda: "synthetic-packaging-fixture")
    monkeypatch.setattr(packager.subprocess, "check_output", checked_output)
    monkeypatch.setattr(sys, "argv", ["package_candidate.py", "--pg-config", "fixture-pg-config",
                                    "--build-dir", str(build), "--proton-root", str(proton),
                                    "--output", str(output)])
    packager.main()
    [candidate] = output.glob("*.zip")
    manifest, members = installer.verified_members(candidate)
    assert manifest["version"] == manifest["distribution_version"] == manifest["native_build_version"] == "0.1.2"
    assert manifest["extensionVersion"] == manifest["sql_default_version"] == "0.1.0"
    assert manifest["sql_available_versions"] == ["0.1.0", "0.1.1", "0.1.2"]
    assert candidate.name.startswith("echoo-pgmq-0.1.2-candidate-")
    assert members["share/extension/echoo_pgmq.control"] == b"default_version = '0.1.0'"
    assert manifest["project_license"] == "Apache-2.0"
    assert manifest["qualification_status"] == "blocked_security_review"
    assert manifest["status"] == "candidate-not-production-release"
    assert members["LICENSE"] == project_license
    assert members["NOTICE"] == project_notice
    for name, content in upstream.items():
        assert members["third-party/proton-" + name] == content


def test_candidate_archive_tampering_fails(tmp_path):
    path = archive(tmp_path)
    with path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="archive SHA256 mismatch"):
        installer.verified_members(path)


def test_candidate_manifest_tampering_fails(tmp_path):
    with pytest.raises(ValueError, match="manifest file list or SHA256 mismatch"):
        installer.verified_members(archive(tmp_path, digest="0" * 64))


@pytest.mark.parametrize("extra", ["candidate/../escape", "candidate/C:/escape", "candidate/\\escape", "/outside/file"])
def test_candidate_unsafe_members_fail(tmp_path, extra):
    with pytest.raises(ValueError, match="unsafe archive member"):
        installer.verified_members(archive(tmp_path, extra=extra))


def test_candidate_unlisted_member_fails(tmp_path):
    with pytest.raises(ValueError, match="manifest file list or SHA256 mismatch"):
        installer.verified_members(archive(tmp_path, extra="candidate/lib/unlisted.so"))
