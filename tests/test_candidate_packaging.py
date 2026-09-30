"""Archive validation does not require PostgreSQL or invoke native code."""
import hashlib
import importlib.util
import json
from pathlib import Path
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
