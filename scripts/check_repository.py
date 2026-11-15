#!/usr/bin/env python3
"""Check local Git exclusions and scan staged/working-tree content without printing secrets."""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    protected = [".env", ".env.company", ".env.db-admin", ".tools/", ".venv/", "参考项目/"]
    for path in protected:
        if subprocess.run(["git", "check-ignore", "--quiet", "--", path], cwd=ROOT).returncode:
            print("FAIL: protected path is not ignored:", path)
            return 1
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, tracked):
        if (
            name in protected
            or name.startswith((".tools/", ".venv/", "参考项目/", ".env."))
            and name != ".env.example"
        ):
            print("FAIL: protected path is tracked:", name)
            return 1
    secrets: set[str] = set()
    for name in (".env", ".env.company", ".env.db-admin"):
        path = ROOT / name
        if not path.exists():
            continue
        if os.name != "nt" and path.stat().st_mode & 0o077:
            print("FAIL: secret file permissions must be 0600:", name)
            return 1
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if (
                sep
                and not key.lstrip().startswith("#")
                and value
                and any(part in key for part in ("API_KEY", "DATABASE_URL", "PASSWORD"))
            ):
                secrets.add(value)
                if "DATABASE_URL" in key and (password := urlsplit(value).password):
                    secrets.add(unquote(password))

    def credential_like(content: bytes) -> bool:
        return (
            any(secret.encode() in content for secret in secrets)
            or re.search(rb"\bsk-[A-Za-z0-9_-]{24,}\b", content) is not None
        )

    # A staged blob can differ from the working tree (for example, a secret staged and
    # then removed locally). Scan the index objects themselves, not only the filesystem.
    staged = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    for record in staged.decode(errors="surrogateescape").split("\0"):
        if not record:
            continue
        metadata, separator, name = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            print("FAIL: malformed staged index entry")
            return 1
        mode, object_id, _stage = fields
        if mode == "120000":
            print("FAIL: review staged symlink before including it:", name)
            return 1
        if mode == "160000" or set(object_id) == {"0"}:
            continue
        try:
            content = subprocess.check_output(["git", "cat-file", "blob", object_id], cwd=ROOT)
        except subprocess.CalledProcessError:
            print("FAIL: cannot read staged object:", name)
            return 1
        if credential_like(content):
            print("FAIL: credential-like material in staged file:", name)
            return 1

    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    for name in sorted(set(filter(None, paths))):
        path = ROOT / name
        if path.is_symlink():
            print("FAIL: review symlink before including it:", name)
            return 1
        if not path.is_file():
            continue
        content = path.read_bytes()
        if credential_like(content):
            print("FAIL: credential-like material in distributable file:", name)
            return 1
    print("PASS: Git exclusions and distributable-file secret scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
