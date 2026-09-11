from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

import httpx

from .config import ROOT, runtime_dir


def client() -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(runtime_dir() / "service.sock")),
                        base_url="http://localhost", timeout=180)


def healthy() -> bool:
    try:
        with client() as c:
            r = c.get("/health", timeout=0.5)
            return r.status_code == 200 and r.json().get("service") == "talk2myagent"
    except httpx.HTTPError:
        return False


def ensure_service():
    if healthy():
        return
    runtime = runtime_dir()
    with (runtime / "startup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy():
            return
        with (runtime / "service.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "talk2myagent", "serve"], cwd=ROOT,
                env={**os.environ, "T2MA_ROOT": str(ROOT)},
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        for _ in range(100):
            if healthy():
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(f"Service did not start. Read {runtime / 'service.log'}.")


def request(operation: str, **arguments) -> dict:
    ensure_service()
    with client() as c:
        response = c.post("/rpc", json={"operation": operation, "arguments": arguments})
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(str(detail))
        return response.json()
