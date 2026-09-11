from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


ROOT = Path(os.environ.get("T2MA_ROOT", Path(__file__).resolve().parents[2])).resolve()


class Settings(BaseModel):
    input_device: str = "BlackHole 16ch"
    output_device: str = "BlackHole 2ch"
    sample_rate: int = 48000
    stt_model: str = "mlx-community/whisper-small.en-mlx"
    voice: str = "af_heart"
    silence_seconds: float = Field(default=0.7, ge=0.2, le=3)
    speech_threshold: float = Field(default=0.008, gt=0, lt=1)
    max_call_seconds: int = Field(default=900, ge=30, le=3600)
    idle_seconds: int = Field(default=120, ge=30, le=600)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def runtime_dir() -> Path:
    return private_dir(ROOT / ".runtime")


def settings() -> Settings:
    p = ROOT / "config.local.json"
    return Settings.model_validate_json(p.read_text()) if p.exists() else Settings()


def write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temp.chmod(0o600)
    temp.replace(path)
