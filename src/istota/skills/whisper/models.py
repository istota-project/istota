"""Model selection and RAM guard for faster-whisper."""

import json
from pathlib import Path

# Approximate RAM requirements per model (GB)
MODEL_REQUIREMENTS = {
    "tiny": 1.0,
    "base": 1.5,
    "small": 2.5,
    "medium": 5.0,
    "large-v3": 10.0,
}


def get_available_memory_gb() -> float:
    """Return available system memory in GB."""
    import psutil

    return psutil.virtual_memory().available / (1024**3)


def select_model(preferred: str | None = None, headroom_gb: float = 1.0) -> str:
    """Select the best model that fits in available RAM.

    If preferred is given and fits, use it. Otherwise pick the largest
    model that leaves at least headroom_gb free.

    Raises ValueError if no model fits or preferred model doesn't fit.
    """
    available = get_available_memory_gb()

    if preferred and preferred != "auto":
        if preferred not in MODEL_REQUIREMENTS:
            raise ValueError(
                f"Unknown model '{preferred}'. "
                f"Available: {', '.join(MODEL_REQUIREMENTS)}"
            )
        needed = MODEL_REQUIREMENTS[preferred]
        if needed + headroom_gb > available:
            raise ValueError(
                f"Model '{preferred}' needs ~{needed:.1f} GB but only "
                f"{available:.1f} GB available (with {headroom_gb:.1f} GB headroom)"
            )
        return preferred

    # Auto-select: pick largest that fits
    for name in reversed(list(MODEL_REQUIREMENTS)):
        needed = MODEL_REQUIREMENTS[name]
        if needed + headroom_gb <= available:
            return name

    raise ValueError(
        f"No model fits in available memory ({available:.1f} GB "
        f"with {headroom_gb:.1f} GB headroom). "
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
