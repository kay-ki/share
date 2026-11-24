import json
from datetime import datetime
from pathlib import Path

DEFAULTS = {
    "ssh_host": "",
    "ssh_port": "",
    "ssh_key": "",
    "ssh_program": "",
    "server_python": "python3",
    "server_script": "",
    "data_root": "",
    "out_root": "",
    "target_res": 1.0,
    "min_hole_area": 4000.0,
    "max_deviation": 3.0,
    "chaikin_iterations": 2,
    "connectivity": 4,
    "nodata_fallback": None,
    "crs_fallback": "",
    "ram_limit_gb": 6.0,
    "blacklist": ["(^|/)processed/"],
    "folder_pattern": "(?P<urheber>[^_]+)_(?P<fahrt>[^_]+)_(?P<datum>[0-9]{4,8})",
    "preview_px": 1000,
}

PARAM_KEYS = ["target_res", "min_hole_area", "max_deviation", "chaikin_iterations",
              "connectivity", "nodata_fallback", "crs_fallback", "ram_limit_gb", "blacklist", "folder_pattern", "preview_px"]
NUMERIC = {"target_res", "min_hole_area", "max_deviation"}
INTEGER = {"chaikin_iterations", "connectivity", "preview_px"}


def config_path():
    return Path.home() / ".polyqa.json"


def log_path():
    return Path.home() / "polyqa.log"


def log(text):
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text.rstrip()}\n\n")


def load():
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg


def save(cfg):
    config_path().write_text(json.dumps(cfg, indent=2))


def params(cfg):
    return {k: cfg[k] for k in PARAM_KEYS}


def configured(cfg):
    return bool(cfg["server_script"] and cfg["data_root"])
