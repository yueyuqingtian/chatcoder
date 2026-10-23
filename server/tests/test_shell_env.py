"""终端 Shell 解析：设置的 Shell 未安装时回退平台默认；Git Bash 不会误认 WSL 的 bash.exe。"""
import sys

import pytest

from app.orchestration.tools import shell_env

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Shell 回退口径只在 Windows 下有意义")


@windows_only
def test_uninstalled_shell_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(shell_env, "_user_config", lambda: {"terminal_shell": "git-bash"})
    monkeypatch.setattr(shell_env, "shell_available", lambda kind: False)
    assert shell_env.resolve_shell() == shell_env.resolve_win_default()


@windows_only
def test_installed_shell_is_used(monkeypatch):
    monkeypatch.setattr(shell_env, "_user_config", lambda: {"terminal_shell": "cmd"})
    monkeypatch.setattr(shell_env, "shell_available", lambda kind: True)
    assert shell_env.resolve_shell() == "cmd.exe"


def test_git_bash_never_resolves_to_wsl_bash(monkeypatch):
    monkeypatch.setattr(shell_env, "_GIT_BASH_CANDIDATES", [])
    monkeypatch.setattr(
        shell_env.shutil, "which",
        lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None,
    )
    assert shell_env.resolve_git_bash() is None
