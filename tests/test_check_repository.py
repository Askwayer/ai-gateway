import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = ".env\n.env.company\n.env.db-admin\n.tools/\n.venv/\n参考项目/\n"


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(IGNORE)
    script = repo / "scripts/check_repository.py"
    script.parent.mkdir()
    shutil.copy2(ROOT / "scripts/check_repository.py", script)
    return repo, script


def _run(repo: Path, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)], cwd=repo, capture_output=True, text=True, timeout=20
    )


def test_staged_secret_is_detected_when_worktree_is_clean(tmp_path: Path) -> None:
    repo, script = _repo(tmp_path)
    secret = "sk-" + "A" * 30
    target = repo / "config.txt"
    target.write_text(secret + "\n")
    subprocess.run(["git", "add", "config.txt"], cwd=repo, check=True)
    target.write_text("clean\n")

    result = _run(repo, script)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "staged" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_untracked_worktree_secret_is_still_detected(tmp_path: Path) -> None:
    repo, script = _repo(tmp_path)
    secret = "sk-" + "B" * 30
    (repo / "config.txt").write_text(secret + "\n")

    result = _run(repo, script)

    assert result.returncode == 1, result.stdout + result.stderr
    assert secret not in result.stdout + result.stderr
