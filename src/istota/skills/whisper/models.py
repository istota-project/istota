"""Model selection and RAM guard for faster-whisper."""

import json
import os
from pathlib import Path

# Approximate RAM requirements per model (GB)
MODEL_REQUIREMENTS = {
    "tiny": 1.0,
    "base": 1.5,
    "small": 2.5,
    "medium": 5.0,
    "large-v3": 10.0,
}

# Default headroom: 0.3 GB. Override via RAM_HEADROOM_MB env var.
_DEFAULT_HEADROOM_GB = 0.3

# Default max model for auto-selection. Override via WHISPER_MAX_MODEL env var.
_DEFAULT_MAX_MODEL = "small"

_MODEL_ORDER = list(MODEL_REQUIREMENTS.keys())


def _get_headroom_gb(override: float | None = None) -> float:
    """Return headroom in GB, checking env var RAM_HEADROOM_MB if no override."""
    if override is not None:
        return override
    env_val = os.environ.get("RAM_HEADROOM_MB")
    if env_val:
        try:
            return float(env_val) / 1024
        except ValueError:
            pass
    return _DEFAULT_HEADROOM_GB


def get_available_memory_gb() -> float:
    """Return available system memory in GB."""
    import psutil

    return psutil.virtual_memory().available / (1024**3)


def _get_max_model() -> str:
    """Return the max model for auto-selection, from env or default."""
    env_val = os.environ.get("WHISPER_MAX_MODEL", "").strip()
    if env_val and env_val in MODEL_REQUIREMENTS:
        return env_val
    return _DEFAULT_MAX_MODEL


def select_model(preferred: str | None = None, headroom_gb: float | None = None) -> str:
    """Select the best model that fits in available RAM.

    If preferred is given and fits, use it. Otherwise pick the largest
    model up to max_model that leaves at least headroom_gb free.

    Max model defaults to 'small'. Override via WHISPER_MAX_MODEL env var.
    Headroom defaults to 0.3 GB. Override via headroom_gb param or
    RAM_HEADROOM_MB env var.

    Raises ValueError if no model fits or preferred model doesn't fit.
    """
    headroom = _get_headroom_gb(headroom_gb)
    available = get_available_memory_gb()

    if preferred and preferred != "auto":
        if preferred not in MODEL_REQUIREMENTS:
            raise ValueError(
                f"Unknown model '{preferred}'. "
                f"Available: {', '.join(MODEL_REQUIREMENTS)}"
            )
        needed = MODEL_REQUIREMENTS[preferred]
        if needed + headroom > available:
            raise ValueError(
                f"Model '{preferred}' needs ~{needed:.1f} GB but only "
                f"{available:.1f} GB available (with {headroom:.1f} GB headroom)"
            )
        return preferred

    # Auto-select: pick largest that fits, capped at max_model
    max_model = _get_max_model()
    max_idx = _MODEL_ORDER.index(max_model)
    candidates = _MODEL_ORDER[: max_idx + 1]

    for name in reversed(candidates):
        needed = MODEL_REQUIREMENTS[name]
        if needed + headroom <= available:
            return name

    raise ValueError(
        f"No model fits in available memory ({available:.1f} GB "
        f"with {headroom:.1f} GB headroom). "
        f"Smallest model (tiny) needs ~{MODEL_REQUIREMENTS['tiny']:.1f} GB."
    )


def list_models() -> list[dict]:
    """List available models with download status and RAM requirements."""
    models = []
    for name, ram_gb in MODEL_REQUIREMENTS.items():
        models.append({
            "name": name,
            "ram_gb": ram_gb,
            "downloaded": _is_model_downloaded(name),
        })
    return models


def download_model(name: str) -> dict:
    """Pre-download a model by loading it once."""
    if name not in MODEL_REQUIREMENTS:
        return {
            "status": "error",
            "error": f"Unknown model '{name}'. Available: {', '.join(MODEL_REQUIREMENTS)}",
        }

    try:
        from faster_whisper import WhisperModel

        WhisperModel(name, device="cpu", compute_type="int8")
        return {"status": "ok", "model": name, "message": f"Model '{name}' downloaded"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _is_model_downloaded(name: str) -> bool:
    """Check if a model exists in the huggingface cache."""
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    if not cache_dir.exists():
        return False
    # faster-whisper models are stored under Systran organization
    model_dir_prefix = f"models--Systran--faster-whisper-{name}"
    return any(d.name == model_dir_prefix for d in cache_dir.iterdir() if d.is_dir())
