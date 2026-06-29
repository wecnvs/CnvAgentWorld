# -*- coding: utf-8 -*-
"""OS별 셸 후보와 POSIX 실행 argv 구성."""
import os
import shlex
import shutil

from config import IS_MAC, IS_WIN


def default_shell():
    if IS_WIN:
        return os.environ.get("COMSPEC") or "powershell.exe"
    if IS_MAC and os.path.exists("/bin/bash"):
        return "/bin/bash"
    return os.environ.get("SHELL") or "/bin/bash"


def shell_candidates():
    out = []
    if IS_WIN:
        for name, path in [
            ("PowerShell", "powershell.exe"),
            ("PowerShell 7", "pwsh.exe"),
            ("Command Prompt", os.environ.get("COMSPEC") or "cmd.exe"),
            ("Git Bash", shutil.which("bash.exe") or ""),
        ]:
            if name in ("PowerShell", "Command Prompt") or shutil.which(path):
                out.append({"name": name, "shell": path})
    else:
        for name, path in [
            ("bash", "/bin/bash"),
            ("zsh", "/bin/zsh"),
            ("Default ($SHELL)", os.environ.get("SHELL") or "/bin/bash"),
        ]:
            if os.path.exists(path) or name.startswith("Default"):
                out.append({"name": name, "shell": path})

    seen, uniq = set(), []
    for item in out:
        if item["shell"] and item["shell"] not in seen:
            seen.add(item["shell"])
            uniq.append(item)
    return uniq


def build_posix_exec_argv(shell_spec: str):
    parts = shlex.split(shell_spec or "")
    shell_path = parts[0] if parts else default_shell()
    extra = parts[1:] if len(parts) > 1 else []
    base = os.path.basename(shell_path)
    if extra:
        return shell_path, [base, *extra]
    return shell_path, [f"-{base}", "-i"]
