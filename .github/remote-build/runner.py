#!/usr/bin/env python3
"""Step scripts executed on the GitHub-hosted runner:  runner.py {fetch|build|deliver|session}

Design rules (this repository is PUBLIC, so the workflow log is public):
  * Source code, build command, build output and artifacts never appear in the workflow log.
    The job spec and source are pulled from your service; build output goes to a private file
    that is uploaded to your service when the build ends.
  * Secrets are only present in the `fetch` and `deliver` steps - never in the `build` step.
  * Standard library only; networking via curl (retries survive a free-tier service waking up).
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

WS = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
STATE = WS / "_rb_state"
SRC = WS / "src"
URL = os.environ.get("RB_SERVICE_URL", "").rstrip("/")
TOKEN = os.environ.get("RB_RUNNER_TOKEN", "")
JOB = os.environ.get("RB_JOB_ID", "")
KIND = os.environ.get("RB_KIND", "build")  # build | session
PREFIX = f"/runner/s/{JOB}" if KIND == "session" else f"/runner/{JOB}"

HEAD_LOG_BYTES = 16 * 1024 * 1024
TAIL_LOG_LINES = 20000


def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------- curl
def curl(endpoint: str, *args: str, max_time: int = 900, query: str = "", prefix: str | None = None,
         token: str | None = None, body: bytes | None = None, retry: bool = True) -> subprocess.CompletedProcess:
    """Call the service. URL/token go through a private temp config file so they never show in `ps`."""
    STATE.mkdir(parents=True, exist_ok=True)
    cfg = STATE / f".curl-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    cfg.write_text(f'url = "{URL}{PREFIX if prefix is None else prefix}/{endpoint}{query}"\n'
                   f'header = "Authorization: Bearer {token or TOKEN}"\n')
    cfg.chmod(0o600)
    cmd = ["curl", "-sS", "-f", "-L", "--connect-timeout", "30", "--max-time", str(max_time)]
    if retry:
        cmd += ["--retry", "12", "--retry-delay", "10", "--retry-connrefused", "--retry-max-time", "420"]
    cmd += ["-K", str(cfg), *args]
    try:
        return subprocess.run(cmd, input=body, capture_output=True)
    finally:
        cfg.unlink(missing_ok=True)


def err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or b"").decode("utf-8", "replace").strip()[-300:]


def load(name: str) -> dict:
    return json.loads((STATE / name).read_text())


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------- fetch
def safe_extract(zip_path: Path, dest: Path, max_mb: int) -> None:
    limit, total = max_mb * 1024 * 1024, 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            p = PurePosixPath(info.filename.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                die("Unsafe path in source archive")
            if ".git" in p.parts:
                continue
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:  # skip symlinks
                continue
            total += info.file_size
            if total > limit:
                die(f"Source expands beyond {max_mb} MB")
            target = dest.joinpath(*p.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if mode & 0o111:  # preserve executable bit (gradlew, scripts)
                target.chmod(0o755)


def cmd_fetch() -> None:
    if not (URL and TOKEN and JOB):
        die("Missing RB_SERVICE_URL / RB_RUNNER_TOKEN / job id (run `python -m app.cli bootstrap`)")
    STATE.mkdir(parents=True, exist_ok=True)
    spec_path = STATE / "spec.json"
    res = curl("spec", "-o", str(spec_path))
    if res.returncode != 0:
        die(f"Could not fetch the job spec from the service: {err(res)}")
    spec = json.loads(spec_path.read_text())
    if KIND == "session":  # keep the per-session token out of spec.json; only the session step reads it
        (STATE / "session.json").write_text(json.dumps({"token": spec.pop("session_token", ""),
                                                        "ttl_minutes": spec.get("ttl_minutes", 60)}))
        (STATE / "session.json").chmod(0o600)
        spec_path.write_text(json.dumps(spec))

    for value in (spec.get("env") or {}).values():  # mask before anything can print them
        for line in str(value).splitlines():
            if len(line.strip()) >= 3:
                print(f"::add-mask::{line.strip()}")

    shutil.rmtree(SRC, ignore_errors=True)
    staging = WS / "_rb_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    source = spec["source"]
    if source["type"] == "upload":
        zpath = STATE / "source.zip"
        res = curl("source", "-o", str(zpath))
        if res.returncode != 0:
            die(f"Could not download the source: {err(res)}")
        safe_extract(zpath, staging, int(spec.get("max_extract_mb", 1000)))
        zpath.unlink(missing_ok=True)
    else:
        clone = ["git", "clone", "--depth", "1"]
        if source.get("ref"):
            clone += ["--branch", source["ref"]]
        out = subprocess.run([*clone, "--", source["url"], str(staging / "repo")], capture_output=True, text=True)
        if out.returncode != 0:
            die("git clone failed (is the repository public and the ref valid?)")
        shutil.rmtree(staging / "repo" / ".git", ignore_errors=True)
        for item in (staging / "repo").iterdir():
            shutil.move(str(item), str(staging / item.name))
        shutil.rmtree(staging / "repo")

    entries = [e for e in staging.iterdir()]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging  # strip single wrapper dir
    shutil.move(str(root), str(SRC))
    shutil.rmtree(staging, ignore_errors=True)

    workdir = (SRC / spec.get("workdir", "")).resolve()
    if SRC.resolve() not in [workdir, *workdir.parents] or not workdir.is_dir():
        die("working_directory does not exist inside the source")

    tc = spec.get("toolchain", {})
    set_output("java_version", tc.get("java_version", "17"))
    set_output("node_version", tc.get("node_version", "22"))
    set_output("python_version", tc.get("python_version", "3.12"))
    set_output("go_version", tc.get("go_version", "stable"))
    set_output("workdir_rel", str(workdir.relative_to(WS)))
    if KIND == "session":
        set_output("step_timeout", str(int(spec.get("ttl_minutes", 60)) + 5))
    else:
        set_output("step_timeout", str(int(spec.get("timeout_minutes", 30)) + 3))
    say("Source ready.")


# ---------------------------------------------------------------------------- session
def cmd_session() -> None:
    """Interactive session: poll the service for commands, run them, stream output back."""
    sess = load("session.json")
    spec = load("spec.json")
    token = sess["token"]
    deadline = time.time() + int(sess.get("ttl_minutes", 60)) * 60
    root = (SRC / spec.get("workdir", "")).resolve()
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("RB_", "INPUT_"))}
    base_env.update({"CI": "true", "TERM": "dumb"})
    base_env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    procs: dict[str, subprocess.Popen] = {}
    cancelled: set[str] = set()
    slots = threading.Semaphore(8)
    sprefix = f"/srunner/{JOB}"

    def S(endpoint: str, *args: str, **kw) -> subprocess.CompletedProcess:
        return curl(endpoint, *args, prefix=sprefix, token=token, **kw)

    def post_output(cid: str, offset: int, data: bytes) -> bool:
        res = S(f"cmd/{cid}/output", "-X", "POST", "-H", "Content-Type: application/octet-stream",
                "--data-binary", "@-", query=f"?offset={offset}", body=data, max_time=60, retry=False)
        return res.returncode == 0

    def post_done(cid: str, payload: dict) -> None:
        S(f"cmd/{cid}/done", "-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-",
          body=json.dumps(payload).encode(), max_time=60)

    def report(cid: str, text: str, code: int) -> None:
        post_output(cid, 0, text.encode("utf-8", "replace"))
        post_done(cid, {"exit_code": code, "timed_out": False, "cancelled": False})

    class Out:
        def __init__(self, cid: str):
            self.cid, self.sent, self.buf, self.lock = cid, 0, bytearray(), threading.Lock()

        def add(self, data: bytes) -> None:
            with self.lock:
                self.buf += data

        def flush(self) -> None:
            with self.lock:
                chunk = bytes(self.buf[:262144])
            if chunk and post_output(self.cid, self.sent, chunk):
                with self.lock:
                    del self.buf[:len(chunk)]
                    self.sent += len(chunk)

        def pending(self) -> bool:
            with self.lock:
                return bool(self.buf)

    def inside(path: Path) -> Path:
        path = path.resolve()
        if root not in [path, *path.parents]:
            raise RuntimeError("path escapes the project directory")
        return path

    def run_exec(c: dict) -> None:
        cid, out = c["cmd_id"], Out(c["cmd_id"])
        stop, finished = threading.Event(), threading.Event()
        started = time.time()
        timed_out = False
        try:
            cwd = inside(root / c.get("cwd", ""))
            env = dict(base_env)
            env.update({k: str(v) for k, v in (c.get("env") or {}).items()})
            proc = subprocess.Popen(["bash", "-o", "pipefail", "-c", c["command"]], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except Exception as exc:  # noqa: BLE001
            report(cid, f"runner error: {exc}\n", 127)
            return
        procs[cid] = proc

        def reader() -> None:
            fd = proc.stdout.fileno()  # type: ignore[union-attr]
            while True:
                data = os.read(fd, 65536)
                if not data:
                    return
                if not finished.is_set():  # after the command ended, just drain so background children never block
                    out.add(data)

        def flusher() -> None:
            while not stop.wait(1.0):
                out.flush()

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=flusher, daemon=True).start()
        try:
            code = proc.wait(timeout=int(c.get("timeout_seconds", 600)))
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            code = 124
        time.sleep(0.5)  # let the reader pick up the last bytes
        finished.set()
        stop.set()
        for _ in range(5):
            out.flush()
            if not out.pending():
                break
            time.sleep(1)
        procs.pop(cid, None)
        was_cancelled = cid in cancelled
        post_done(cid, {"exit_code": code, "timed_out": timed_out, "cancelled": was_cancelled,
                        "seconds": round(time.time() - started)})

    def run_put(c: dict) -> None:
        cid = c["cmd_id"]
        dest = inside(root / c.get("dest", ""))
        zpath = STATE / f"put-{cid}.zip"
        res = S(f"upload/{c['upload_id']}", "-o", str(zpath), max_time=600)
        if res.returncode != 0:
            report(cid, f"download of the upload failed: {err(res)}\n", 1)
            return
        staging = WS / f"_rb_put_{cid}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        safe_extract(zpath, staging, int(spec.get("max_extract_mb", 1000)))
        entries = list(staging.iterdir())
        top = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        dest.mkdir(parents=True, exist_ok=True)
        removed = 0
        if c.get("mode") == "replace":
            keep = set(c.get("keep") or [])
            for item in list(dest.iterdir()):
                if item.name in keep:
                    continue
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() and not item.is_symlink() else item.unlink(missing_ok=True)
                removed += 1
        count = 0
        for src in top.rglob("*"):
            target = dest / src.relative_to(top)
            if src.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                count += 1
        shutil.rmtree(staging, ignore_errors=True)
        zpath.unlink(missing_ok=True)
        report(cid, f"Updated {count} file(s) in '{c.get('dest') or '.'}' (mode={c.get('mode')}, removed {removed} old entries).\n", 0)

    def run_collect(c: dict) -> None:
        cid = c["cmd_id"]
        base = inside(root / c.get("cwd", ""))
        files = collect(c["paths"], base)
        if not files:
            report(cid, "No files matched. Paths are globs relative to the project (or cwd); check them with `ls`/`find`.\n", 1)
            return
        pkg = package(files, base, c.get("package", "auto"), c.get("name") or "session-files", exact_name=bool(c.get("name")))
        if len(files) == 1 and c.get("package", "auto") == "auto" and c.get("name"):
            renamed = STATE / re.sub(r"[^A-Za-z0-9._-]", "-", c["name"])
            shutil.copy2(pkg, renamed)
            pkg = renamed
        size = pkg.stat().st_size
        if size > int(spec.get("max_artifact_mb", 200)) * 1024 * 1024:
            report(cid, f"Artifact is {size // 1048576} MB, above the service limit of {spec.get('max_artifact_mb')} MB.\n", 1)
            return
        digest = sha256(pkg)
        resp = STATE / f"collect-{cid}.json"
        up = S("artifact", "-X", "PUT", "-T", str(pkg), "-o", str(resp), max_time=1800,
               query="?filename=" + urllib.parse.quote(pkg.name))
        if up.returncode != 0:
            report(cid, f"upload to the service failed: {err(up)}\n", 1)
            return
        reply = json.loads(resp.read_text())
        if reply.get("sha256") != digest:
            report(cid, "checksum mismatch after upload\n", 1)
            return
        listing = "\n".join(f"  {f.relative_to(base).as_posix()} ({f.stat().st_size} bytes)" for f in files[:50])
        report(cid, f"Collected {len(files)} file(s) -> {reply['filename']} ({size} bytes, sha256 {digest})\n{listing}\n", 0)

    def guarded(fn, c: dict) -> None:
        try:
            fn(c)
        except BaseException as exc:  # noqa: BLE001  (SystemExit from die() included)
            try:
                report(c["cmd_id"], f"runner error: {exc}\n", 1)
            except Exception:  # noqa: BLE001
                pass
        finally:
            slots.release()

    say("Session started.")
    last_ok = time.time()
    while time.time() < deadline:
        res = S("next", query="?wait=25", max_time=45, retry=False)
        if res.returncode != 0:
            if time.time() - last_ok > 300:
                say("::warning::Lost contact with the service; ending the session.")
                break
            time.sleep(5)
            continue
        last_ok = time.time()
        try:
            msg = json.loads(res.stdout or b"{}")
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "close":
            break
        if kind == "cancel":
            cancelled.add(msg["cmd_id"])
            proc = procs.get(msg["cmd_id"])
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        elif kind in ("exec", "put", "collect"):
            handler = {"exec": run_exec, "put": run_put, "collect": run_collect}[kind]
            slots.acquire()
            threading.Thread(target=guarded, args=(handler, msg), daemon=True).start()
    for proc in list(procs.values()):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)
    say("Session ended.")


# ---------------------------------------------------------------------------- build
def cmd_build() -> None:
    spec = load("spec.json")
    workdir = (SRC / spec.get("workdir", "")).resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RB_", "INPUT_"))}
    env.update({"CI": "true", "TERM": "dumb"})
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    timeout_s = int(spec.get("timeout_minutes", 30)) * 60

    say("Build started.")
    started = time.time()
    proc = subprocess.Popen(["bash", "-eo", "pipefail", "-c", spec["command"]], cwd=workdir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    log_path = STATE / "build.log"
    tail: collections.deque[bytes] = collections.deque(maxlen=TAIL_LOG_LINES)
    state = {"written": 0, "truncated": False}

    def pump() -> None:
        with open(log_path, "wb") as fh:
            for line in proc.stdout:  # type: ignore[union-attr]
                if state["written"] < HEAD_LOG_BYTES:
                    fh.write(line)
                    state["written"] += len(line)
                else:
                    state["truncated"] = True
                    tail.append(line)
            if state["truncated"]:
                fh.write(b"\n[... middle of the log omitted (too large); last lines follow ...]\n")
                fh.writelines(tail)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=15)
                break
            except subprocess.TimeoutExpired:
                continue
        code = 124
    reader.join(timeout=30)
    seconds = round(time.time() - started)
    (STATE / "build.json").write_text(json.dumps({"exit_code": code, "timed_out": timed_out, "seconds": seconds}))
    say(f"Build finished: exit={code} timed_out={timed_out} seconds={seconds}")  # no build output in the public log


# -------------------------------------------------------------------------- deliver
def collect(patterns: list[str], workdir: Path) -> list[Path]:
    import glob

    found: dict[str, Path] = {}
    for pattern in patterns:
        for match in glob.glob(pattern, root_dir=str(workdir), recursive=True):
            path = (workdir / match).resolve()
            if workdir not in [path, *path.parents]:
                continue
            candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
            for p in candidates:
                if p.is_file() and not p.is_symlink():
                    found[str(p)] = p
    files = sorted(found.values())
    if len(files) > 5000:
        raise RuntimeError("artifact_paths matched more than 5000 files")
    return files


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def package(files: list[Path], workdir: Path, mode: str, label: str, exact_name: bool = False) -> Path:
    if len(files) == 1 and mode == "auto":
        return files[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:40] or "build"
    out = STATE / (f"{slug}.zip" if exact_name else f"{slug}-{JOB}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            zf.write(f, f.relative_to(workdir).as_posix())
    return out


def cmd_deliver() -> None:
    spec = load("spec.json")
    build_file = STATE / "build.json"
    result: dict = {"label": spec.get("label"), "language": spec.get("language"),
                    "exit_code": None, "timed_out": False, "artifacts": [], "ok": False}
    if not build_file.exists():
        result["reason"] = "A setup step failed before the build command ran (open the run page to see which)."
    else:
        build = json.loads(build_file.read_text())
        result.update(exit_code=build["exit_code"], timed_out=build["timed_out"], build_seconds=build["seconds"])
        if build["exit_code"] == 0 and not build["timed_out"]:
            if not spec.get("return_artifacts", True):
                result.update(ok=True, reason="Build succeeded (no artifact requested).")
            else:
                try:
                    workdir = (SRC / spec.get("workdir", "")).resolve()
                    files = collect(spec["artifacts"], workdir)
                    if not files:
                        raise RuntimeError("artifact_paths matched no files (check the glob; paths are relative to the project/working_directory)")
                    result["artifacts"] = [{"path": f.relative_to(workdir).as_posix(), "size": f.stat().st_size}
                                           for f in files[:200]]
                    pkg = package(files, workdir, spec.get("package", "auto"), spec.get("label") or "build")
                    size = pkg.stat().st_size
                    if size > int(spec.get("max_artifact_mb", 200)) * 1024 * 1024:
                        raise RuntimeError(f"artifact is {size // 1048576} MB, above the service limit of {spec.get('max_artifact_mb')} MB")
                    digest = sha256(pkg)
                    up = curl("artifact", "-X", "PUT", "-T", str(pkg), "-o", str(STATE / "upload.json"),
                              max_time=1800, query="?filename=" + urllib.parse.quote(pkg.name))
                    if up.returncode != 0:
                        raise RuntimeError(f"upload to the service failed: {err(up)}")
                    reply = json.loads((STATE / "upload.json").read_text())
                    if reply.get("sha256") != digest:
                        raise RuntimeError("artifact checksum mismatch after upload")
                    result.update(artifact={"filename": reply["filename"], "size_bytes": size, "sha256": digest,
                                            "kind": "file" if pkg in files else "zip"},
                                  package="file" if pkg in files else "zip", ok=True,
                                  reason="Build finished; the artifact is ready to download.")
                except Exception as exc:  # noqa: BLE001
                    result["delivery_error"] = str(exc)[:300]
        elif build["timed_out"]:
            result["reason"] = "Build timed out."

    (STATE / "result.json").write_text(json.dumps(result))
    args = ["-X", "POST", "-F", f"result=<{STATE / 'result.json'}"]
    if (STATE / "build.log").exists():
        args += ["-F", f"log=@{STATE / 'build.log'}"]
    post = curl("complete", *args, max_time=300)
    if post.returncode != 0:
        say(f"::warning::Could not report to the service: {err(post)}")
    # Minimal, non-sensitive summary so the result can be recovered from the run log if the service restarted.
    public = {"ok": result["ok"], "exit_code": result["exit_code"], "timed_out": result["timed_out"],
              "build_seconds": result.get("build_seconds"), "language": result["language"],
              "delivery_error": "artifact step failed" if result.get("delivery_error") else None}
    say("RB_RESULT " + json.dumps(public, separators=(",", ":")))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    commands = {"fetch": cmd_fetch, "build": cmd_build, "deliver": cmd_deliver, "session": cmd_session}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        die("usage: runner.py fetch|build|deliver|session")
    commands[sys.argv[1]]()
