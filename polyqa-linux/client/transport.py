import json
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
BUNDLED_SSH = APP_DIR / "openssh" / "ssh.exe"


def ssh_program(cfg):
    if cfg["ssh_program"]:
        return cfg["ssh_program"]
    if shutil.which("ssh") or not BUNDLED_SSH.exists():
        return "ssh"
    return str(BUNDLED_SSH)


def ssh_command(cfg, remote_args):
    if not cfg["ssh_host"]:
        return remote_args
    base = [ssh_program(cfg), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new"]
    if cfg["ssh_port"]:
        base += ["-p", str(cfg["ssh_port"])]
    if cfg["ssh_key"]:
        base += ["-i", cfg["ssh_key"]]
    base.append(cfg["ssh_host"])
    base.append(" ".join(shlex.quote(a) for a in remote_args))
    return base


def call(cfg, command, on_progress=None, **extra):
    remote = [cfg["server_python"], "-u", cfg["server_script"], command,
              "--data", cfg["data_root"], "--out", cfg["out_root"],
              "--params", json.dumps(settings.params(cfg))]
    for key, value in extra.items():
        if value is True:
            remote.append(f"--{key}")
        elif value is not False:
            remote += [f"--{key}", str(value)]
    process = subprocess.Popen(ssh_command(cfg, remote), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(process.stdout.read()))
    reader.start()
    noise = []
    for line in process.stderr:
        line = line.rstrip()
        if line.startswith("PROGRESS "):
            if on_progress:
                _, index, total, stem = line.split(" ", 3)
                on_progress(int(index), int(total), stem)
        elif line:
            noise.append(line)
    reader.join()
    process.wait()
    out = (chunks[0] or "").strip()
    if not out:
        detail = " / ".join(noise[-3:]) or f"Aufruf endet mit Code {process.returncode}"
        raise RuntimeError(detail)
    result = json.loads(out)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result
