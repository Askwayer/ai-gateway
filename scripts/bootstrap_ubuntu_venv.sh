#!/usr/bin/env bash
# Run on the Ubuntu target. Does not install OS packages or alter system Python.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
system_python=/usr/bin/python3

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo 'Expected Ubuntu 24.04 on x86_64 (amd64); no environment changes made.' >&2
    exit 1
fi

if [[ ! -x "$system_python" ]]; then
    echo 'Missing /usr/bin/python3; provision the Ubuntu Python packages first.' >&2
    exit 1
fi

"$system_python" - <<'PY'
import platform
import sys

release = platform.freedesktop_os_release()
if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04':
    raise SystemExit('Expected Ubuntu 24.04; no environment changes made.')
if not ((3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)):
    raise SystemExit('Expected system Python >=3.12.3,<3.13; system Python will not be replaced.')
print('Target:', release.get('PRETTY_NAME'), platform.machine())
print('System Python:', platform.python_version())
PY

if [[ -e "$project_root/.venv" || -L "$project_root/.venv" ]]; then
    if [[ ! -x "$project_root/.venv/bin/python" ]]; then
        echo 'Existing .venv is invalid; inspect it before recreating. Nothing overwritten.' >&2
        exit 1
    fi
    "$project_root/.venv/bin/python" - "$project_root/.venv" <<'PY'
import pathlib
import sys
if pathlib.Path(sys.prefix).resolve() != pathlib.Path(sys.argv[1]).resolve():
    raise SystemExit('Existing .venv resolves elsewhere; nothing overwritten.')
if sys.prefix == sys.base_prefix or not ((3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)):
    raise SystemExit('Existing .venv is not an isolated Python 3.12 environment; nothing overwritten.')
if pathlib.Path(sys._base_executable).resolve() != pathlib.Path('/usr/bin/python3').resolve():
    raise SystemExit('Existing .venv does not use Ubuntu system Python; nothing overwritten.')
PY
else
    if ! "$system_python" -m venv "$project_root/.venv"; then
        echo 'venv creation failed. Check python3.12-venv availability and disk permissions.' >&2
        echo 'In an approved package source, the OS prerequisite is: apt install python3.12-venv' >&2
        echo 'A partial .venv may exist; inspect it before retrying.' >&2
        exit 1
    fi
fi

"$project_root/.venv/bin/python" --version
"$project_root/.venv/bin/python" -m pip --version
echo 'Python environment ready. Dependencies must be installed from the locked requirements.'
printf 'Activate with: source "%s/.venv/bin/activate"\n' "$project_root"
