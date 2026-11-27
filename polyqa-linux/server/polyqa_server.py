import argparse
import base64
import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def system_shapely_major():
    try:
        return int(version("shapely").split(".")[0])
    except PackageNotFoundError:
        return 0


# Debian 12 ships Shapely 1.8, the code needs 2.x; vendor/ holds 2.0.7 built for Python 3.11
if system_shapely_major() < 2:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

import render
import steps

RASTER_SUFFIXES = {".tif", ".tiff", ".png"}
RASTER_STEPS = [k for k in steps.STEP_KEYS if k != "merge"]
MARKS = {"ok", "bad"}
THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "GDAL_NUM_THREADS": "1"}


def roots(args):
    data = Path(args.data).expanduser().resolve()
    out = Path(args.out).expanduser() if args.out else data / "polyqa_out"
    return data, data / "polyqa_work", out


def with_stems(folder, files):
    names = ["__".join(f.relative_to(folder).with_suffix("").parts) for f in files]
    dupes = {n for n in names if names.count(n) > 1}
    return [(f, f"{n}_{f.suffix.lower()[1:]}" if n in dupes else n) for f, n in zip(files, names)]


def collect(data, blacklist):
    patterns = [re.compile(p) for p in blacklist]
    work, out = data / "polyqa_work", data / "polyqa_out"
    folders = []
    for d in sorted(p for p in data.iterdir() if p.is_dir() and p not in (work, out)):
        files = [f for f in sorted(d.rglob("*"))
                 if f.is_file() and f.suffix.lower() in RASTER_SUFFIXES
                 and not any(p.search(str(f.relative_to(data))) for p in patterns)]
        if files:
            folders.append((d, with_stems(d, files)))
    return folders


def find_folder(data, blacklist, name):
    for d, rasters in collect(data, blacklist):
        if d.name == name:
            return d, rasters
    raise RuntimeError(f"Ordner {name} nicht gefunden")


def open_folder(args):
    data, work_root, out_root = roots(args)
    folder, rasters = find_folder(data, args.params.get("blacklist", []), args.folder)
    return folder, rasters, work_root / folder.name, out_root


def step_state(work, step, stems):
    keys = [steps.FOLDER_STEM] if step == "merge" else stems
    found = [steps.read_metrics(work, s).get(step) for s in keys]
    if not all(found):
        return "offen"
    return "flag" if any(m.get("flag") for m in found) else "fertig"


def job_file(work):
    return work / "job.json"


def read_job(work):
    p = job_file(work)
    return json.loads(p.read_text()) if p.exists() else None


def write_job(work, job):
    work.mkdir(parents=True, exist_ok=True)
    tmp = job_file(work).with_suffix(".part")
    tmp.write_text(json.dumps(job))
    os.replace(tmp, job_file(work))


def alive(pid):
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"polyqa_server" in cmd and b"job" in cmd


def job_status(work):
    job = read_job(work)
    if not job:
        return None
    job["running"] = not job["finished"] and alive(job["pid"])
    job["broken"] = not job["finished"] and not job["running"]
    return job


def review_file(work):
    return work / "review.json"


def read_review(work):
    p = review_file(work)
    return json.loads(p.read_text()) if p.exists() else {}


def write_review(work, review):
    work.mkdir(parents=True, exist_ok=True)
    tmp = review_file(work).with_suffix(".part")
    tmp.write_text(json.dumps(review, indent=1))
    os.replace(tmp, review_file(work))


def summary(work, stems):
    review = read_review(work)
    done = issues = 0
    for stem in stems:
        found = steps.read_metrics(work, stem)
        done += all(k in found for k in RASTER_STEPS)
        issues += any(v.get("flag") for v in found.values())
    marks = [review.get(s) for s in stems]
    return {"total": len(stems), "done": done, "issues": issues,
            "good": marks.count("ok"), "bad": marks.count("bad")}


def cmd_discover(args):
    data, work_root, _ = roots(args)
    folders = []
    for d, rasters in collect(data, args.params.get("blacklist", [])):
        stems = [stem for _, stem in rasters]
        folders.append({"name": d.name, "rasters": [str(r) for r, _ in rasters], "stems": stems,
                        "summary": summary(work_root / d.name, stems)})
    return {"steps": steps.STEPS, "folders": folders, "folder_stem": steps.FOLDER_STEM}


def cmd_state(args):
    _, rasters, work, _ = open_folder(args)
    stems = [stem for _, stem in rasters]
    metrics = {}
    for stem in [*stems, steps.FOLDER_STEM]:
        payload = steps.read_metrics(work, stem)
        metrics[stem] = {k: {f: v[f] for f in ("gauges", "before", "after", "flag", "trace") if f in v}
                         for k, v in payload.items()}
    return {"steps": {k: step_state(work, k, stems) for k in steps.STEP_KEYS},
            "metrics": metrics, "job": job_status(work),
            "review": read_review(work), "summary": summary(work, stems)}


def cmd_mark(args):
    _, rasters, work, _ = open_folder(args)
    stems = [stem for _, stem in rasters]
    if args.stem not in stems:
        raise RuntimeError(f"Raster {args.stem} nicht im Ordner")
    review = read_review(work)
    if args.value in MARKS:
        review[args.stem] = args.value
    else:
        review.pop(args.stem, None)
    write_review(work, review)
    return {"review": review, "summary": summary(work, stems)}


def clear_step(work, stems, step):
    first = steps.STEP_KEYS.index(step)
    for later in steps.STEP_KEYS[first:]:
        for stem in [*stems, steps.FOLDER_STEM]:
            render.clear(work, stem, later)
    for stem in ([steps.FOLDER_STEM] if step == "merge" else stems):
        payload = steps.read_metrics(work, stem)
        if payload.pop(step, None) is not None:
            steps.store_metrics(work, stem, payload)
    if step != "merge":
        review = read_review(work)
        if any(s in review for s in stems):
            write_review(work, {k: v for k, v in review.items() if k not in stems})


def error_payload(exc):
    text = "Arbeitsspeicher-Grenze erreicht" if isinstance(exc, MemoryError) else (str(exc) or type(exc).__name__)
    return {"path": None, "flag": True, "before": "", "after": "nicht gelaufen",
            "gauges": [["Fehler", text, "flag"], ["", "", ""], ["", "", ""]],
            "trace": traceback.format_exc()}


def log_failure(step, stem, payload):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {step} {stem}\n{payload['trace']}", flush=True)


def run_raster(step, raster, work, stem, p):
    if step == "down":
        return steps.down(raster, work, stem, p["target_res"], p.get("crs_fallback"))
    if step == "grey":
        return steps.grey(work, stem, p["nodata_fallback"])
    if step == "mask":
        return steps.mask(work, stem)
    if step == "poly":
        return steps.polygonize(work, stem, p["connectivity"])
    if step == "holes":
        return steps.close_holes(work, stem, p["min_hole_area"])
    return steps.smooth(work, stem, p["max_deviation"], p["chaikin_iterations"])


def run_step(folder, rasters, work, out_root, step, p, progress):
    if step == "merge":
        try:
            payload = steps.merge(work, out_root, folder.name, [s for _, s in rasters], p["folder_pattern"])
        except Exception as exc:
            payload = error_payload(exc)
            log_failure(step, steps.FOLDER_STEM, payload)
        steps.write_metrics(work, steps.FOLDER_STEM, "merge", payload)
        return payload["flag"]
    flagged = False
    for i, (raster, stem) in enumerate(rasters):
        progress(i + 1, len(rasters), stem)
        done = steps.read_metrics(work, stem).get(step)
        if done is None:
            try:
                done = run_raster(step, raster, work, stem, p)
            except Exception as exc:
                done = error_payload(exc)
                log_failure(step, stem, done)
            steps.write_metrics(work, stem, step, done)
        flagged = flagged or done["flag"]
    return flagged


def limit_resources(p):
    os.nice(19)
    try:
        subprocess.run(["ionice", "-c2", "-n7", "-p", str(os.getpid())], check=False)
    except FileNotFoundError:
        pass
    if p.get("ram_limit_gb"):
        size = int(float(p["ram_limit_gb"]) * 1024 ** 3)
        resource.setrlimit(resource.RLIMIT_DATA, (size, size))
    steps.gdal.SetCacheMax(256 * 1024 * 1024)


def cmd_start(args):
    folder, rasters, work, _ = open_folder(args)
    running = job_status(work)
    if running and running["running"]:
        raise RuntimeError(f"für {folder.name} läuft schon eine Rechnung")
    work.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "job", "--data", args.data,
           "--out", args.out, "--folder", args.folder, "--step", args.step,
           "--params", json.dumps(args.params)] + (["--fresh"] if args.fresh else [])
    with open(work / "job.log", "a") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, env={**os.environ, **THREAD_ENV})
    write_job(work, {"pid": proc.pid, "steps": [s for s in args.step.split(",") if s], "done": [], "step": None,
                     "index": 0, "total": 0, "stem": "", "flag": None, "error": None, "trace": None, "finished": False})
    return {"started": proc.pid}


def cmd_job(args):
    folder, rasters, work, out_root = open_folder(args)
    limit_resources(args.params)
    stems = [s for _, s in rasters]
    # start writes job.json after spawning; wait for it so neither side overwrites the other
    for _ in range(100):
        job = read_job(work) or {}
        if job.get("pid") == os.getpid():
            break
        time.sleep(0.1)
    job.update({"pid": os.getpid(), "steps": [s for s in args.step.split(",") if s], "done": [], "flag": None,
                "error": None, "trace": None, "finished": False})

    def progress(i, n, stem):
        job.update({"index": i, "total": n, "stem": stem})
        write_job(work, job)

    try:
        for step in job["steps"]:
            job.update({"step": step, "index": 0, "total": 0, "stem": ""})
            write_job(work, job)
            if args.fresh:
                clear_step(work, stems, step)
            flagged = run_step(folder, rasters, work, out_root, step, args.params, progress)
            job["done"].append(step)
            if flagged:
                job["flag"] = step
                break
    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["trace"] = traceback.format_exc()
        traceback.print_exc()
    job["finished"] = True
    write_job(work, job)
    return {"finished": True}


def cmd_status(args):
    _, _, work, _ = open_folder(args)
    return {"job": job_status(work)}


def cmd_stop(args):
    _, _, work, _ = open_folder(args)
    job = job_status(work)
    if job and job["running"]:
        os.killpg(job["pid"], signal.SIGTERM)
    return {"stopped": bool(job and job["running"])}


def cmd_preview(args):
    _, rasters, work, _ = open_folder(args)
    source = next((str(r) for r, stem in rasters if stem == args.stem), None)
    try:
        path = render.render(work, [s for _, s in rasters], args.stem, args.step, args.side,
                             int(args.params.get("preview_px", 1000)), source)
    except Exception as exc:
        return {"png": None, "note": f"{type(exc).__name__}: {exc}"}
    if path is None:
        return {"png": None, "note": "nichts zu zeigen"}
    return {"png": base64.b64encode(Path(path).read_bytes()).decode("ascii")}


COMMANDS = {"discover": cmd_discover, "state": cmd_state, "start": cmd_start, "job": cmd_job,
            "status": cmd_status, "stop": cmd_stop, "preview": cmd_preview, "mark": cmd_mark}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--folder", default="")
    parser.add_argument("--stem", default="")
    parser.add_argument("--step", default="")
    parser.add_argument("--side", default="")
    parser.add_argument("--params", default="{}")
    parser.add_argument("--value", default="")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    args.params = json.loads(args.params)
    try:
        result = COMMANDS[args.command](args)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
