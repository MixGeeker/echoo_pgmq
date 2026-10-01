#!/usr/bin/env python3
"""Apply Echoo's narrow MSVC/OpenSSL build fix to verified Apache Proton 0.40.0.

Original source remains Apache-2.0 licensed. Its copyright/license headers are
preserved. This does not disable warnings, TLS checks, or certificate validation.
Run only after fetch_dependency.py has verified the official source archive.
"""
import argparse
import hashlib
from pathlib import Path

INPUTS = {
    "c/src/ssl/openssl.c": "ecf3a5f967bab475a67c057bc7266cdeac3bf26d74355ada680e00693650b31e",
    "c/src/tls/openssl.c": "c3e9195adada707fdb3e7c36b6f2743a34e6ce7dc76ec9bd11097450d36602cf",
}


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise SystemExit(f"Expected exactly one patch anchor: {before!r}")
    return text.replace(before, after, 1)


def patched(text):
    text = replace_once(text, "#define _WIN32_WINNT 0x0501", "#define _WIN32_WINNT 0x0600")
    text = replace_once(text, '#if _WIN32_WINNT < 0x0501\n#error "Proton requires Windows API support for XP or later."',
                        '#if _WIN32_WINNT < 0x0600\n#error "Proton OpenSSL requires Windows Vista or later (InitOnceExecuteOnce)."')
    text = replace_once(text, '#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wdeprecated-declarations"',
                        '#if defined(__GNUC__) || defined(__clang__)\n#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wdeprecated-declarations"\n#endif')
    text = replace_once(text, '#pragma GCC diagnostic pop',
                        '#if defined(__GNUC__) || defined(__clang__)\n#pragma GCC diagnostic pop\n#endif')
    text = replace_once(text, 'INIT_ONCE initialize_once = INIT_ONCE_STATIC_INIT;', '''/* Echoo build patch: use the Windows INIT_ONCE callback ABI. */
static void initialize(void);
static BOOL CALLBACK initialize_windows(PINIT_ONCE once, PVOID parameter, PVOID *context) {
  (void)once;
  (void)parameter;
  (void)context;
  initialize();
  return init_ok ? TRUE : FALSE;
}
static INIT_ONCE initialize_once = INIT_ONCE_STATIC_INIT;''')
    text = replace_once(text, '  void* dummy;\n  InitOnceExecuteOnce(&initialize_once, &initialize, NULL, &dummy);\n  return init_ok;',
                        '  return InitOnceExecuteOnce(&initialize_once, &initialize_windows, NULL, NULL) && init_ok;')
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    updates = []
    for relative, expected in INPUTS.items():
        path = args.source / relative
        original = path.read_bytes()
        actual = hashlib.sha256(original).hexdigest()
        if actual != expected:
            raise SystemExit(f"Refusing unknown or already patched source {relative}: {actual}")
        content = patched(original.decode("utf-8")).encode("utf-8")
        updates.append((path, content))
        print(f"{relative}: verified {actual}; patched {hashlib.sha256(content).hexdigest()}")
    for path, content in updates:
        path.write_bytes(content)
    (args.source / "ECHOO_WINDOWS_OPENSSL_PATCH.txt").write_text(
        "Apache Qpid Proton 0.40.0 OpenSSL source modified by Echoo build tooling.\n"
        "Original Apache-2.0 license and notices remain in LICENSE.txt and NOTICE.txt.\n"
        "Changes: require Vista+ API for InitOnceExecuteOnce, use its documented BOOL CALLBACK ABI, "
        "and guard GCC-only diagnostic pragmas. TLS verification and protocol logic are unchanged.\n"
        "Reproduce with scripts/fetch_dependency.py then scripts/patch_proton_windows.py.\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
