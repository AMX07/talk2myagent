from __future__ import annotations

import fcntl
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, validate_call

from .config import runtime_dir, settings
from .engine import Engine


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    arguments: dict = Field(default_factory=dict)


def create_app(engine: Engine) -> FastAPI:
    operations = {name: validate_call(getattr(engine, name)) for name in (
        "doctor", "prepare", "dial_request", "connect", "recording_start", "recording_stop",
        "say", "listen", "keypad", "tones", "interrupt", "simulate_remote", "finish", "result",
    )}

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.shutdown()

    app = FastAPI(title="talk2myagent local service", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"ok": True, "service": "talk2myagent", "version": "0.1.0"}

    @app.post("/rpc")
    def rpc(request: Request):
        if request.operation not in operations:
            raise HTTPException(400, "Unknown operation")
        try:
            return operations[request.operation](**request.arguments)
        except (ValueError, TypeError, ValidationError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return app


def serve():
    os.umask(0o077)
    runtime = runtime_dir()
    # One service owns the hardware; never unlink another live service's socket.
    with (runtime / "service.lock").open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("talk2myagent service is already running.")
        socket = runtime / "service.sock"
        socket.unlink(missing_ok=True)
        uvicorn.run(create_app(Engine(settings())), uds=str(socket), log_level="warning")
