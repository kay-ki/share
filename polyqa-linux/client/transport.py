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


def has_gdal(python):
    try:
        return subprocess.run([python, "-c", "from osgeo import gdal"], capture_output=True,
                              timeout=30, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def fill_local(cfg):
    script = APP_DIR / "server" / "polyqa_server.py"
    if cfg["ssh_host"] or cfg["server_script"] or not script.exists():
        return cfg
    wrapper = str(APP_DIR / "server" / "python3-polyqa")
    python = "python3" if has_gdal("python3") or not has_gdal(wrapper) else wrapper
    return {**cfg, "server_script": str(script), "server_python": python}


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
    command_line = ssh_command(cfg, remote)
    process = subprocess.Popen(command_line, stdout=subprocess.PIPE,
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
        text = (f"{noise[-1]}\n\n" + "\n".join(noise)) if noise else f"Aufruf endet mit Code {process.returncode}"
        fail(command, command_line, text)
    result = json.loads(out)
    if "error" in result:
        fail(command, command_line, "\n\n".join(t for t in (result["error"], result.get("trace")) if t))
    return result


def fail(command, command_line, text):
    settings.log(f"{command}: {text}\nBefehl: {' '.join(shlex.quote(a) for a in command_line)}")
    raise RuntimeError(text)
