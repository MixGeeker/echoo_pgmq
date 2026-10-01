#!/usr/bin/env python3
"""Generate an ephemeral local CA. Never use these credentials in production."""
import argparse
import os
from pathlib import Path
import subprocess


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    if os.name != "nt":
        directory.chmod(0o700)

    def openssl(*args):
        subprocess.run([os.environ.get("ECHOO_TEST_OPENSSL", "openssl"), *args], cwd=directory, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "2",
            "-subj", "/CN=Echoo disposable CI CA", "-keyout", "ca.key", "-out", "ca.pem",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name, cn, usage in [("server", "localhost", "serverAuth"),
                             ("client", "echoo_test_user", "clientAuth"),
                             ("unknown", "echoo_unknown", "clientAuth")]:
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", f"/CN={cn}",
                "-keyout", f"{name}.key", "-out", f"{name}.csr")
        extensions = "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        extensions += f"extendedKeyUsage={usage}\n"
        if name == "server":
            extensions += "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        (directory / f"{name}.ext").write_text(extensions, encoding="ascii")
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                "-CAcreateserial", "-out", f"{name}.pem", "-days", "2", "-sha256",
                "-extfile", f"{name}.ext")
        # Native Windows Proton/SChannel takes PKCS#12, not PEM. Empty password is
        # intentional ONLY for this disposable local test PKI.
        openssl("pkcs12", "-export", "-in", f"{name}.pem", "-inkey", f"{name}.key",
                "-certfile", "ca.pem", "-name", name, "-passout", "pass:", "-out", f"{name}.p12")
    openssl("pkcs12", "-export", "-nokeys", "-in", "ca.pem", "-name", "ca", "-passout", "pass:",
            "-out", "ca.p12")
    if os.name != "nt":
        for path in directory.iterdir():
            path.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory.resolve())
