"""Optional local sentence embeddings for personal-model retrieval.

The implementation is deliberately fail-open: missing model files or optional ONNX/tokenizer
dependencies make ``available`` false and ``embed`` return ``None``. Lexical retrieval
continues unchanged.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logger import get

if TYPE_CHECKING:
    import numpy as np

logger = get("persome.retrieval.local_embeddings")

_MODEL_SUBDIR = "bge-small-zh"
_ONNX_NAME = "model.int8.onnx"
_TOKENIZER_NAME = "tokenizer.json"
_MAX_LEN_FALLBACK = 256
_CACHE_MAX = 512
_MISS = object()

_engine_lock = threading.Lock()
_engine: _Engine | None = None
_load_failed = False
_cache: OrderedDict[str, object] = OrderedDict()
_cache_lock = threading.Lock()


def _models_root() -> Path | None:
    env = os.environ.get("PERSOME_GATE_MODEL_DIR") or os.environ.get("MENS_CONTEXT_GATE_MODEL_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "gate_models"  # type: ignore[attr-defined]
    vendored = Path(__file__).resolve().parents[3] / "gate_models"
    return vendored if vendored.exists() else None


def _model_dir() -> Path | None:
    root = _models_root()
    if root is None:
        return None
    directory = root / _MODEL_SUBDIR
    needed = [directory / _ONNX_NAME, directory / _TOKENIZER_NAME]
    return directory if all(path.exists() for path in needed) else None


class _Engine:
    def __init__(self, model_dir: Path) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / _TOKENIZER_NAME))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_dir / _ONNX_NAME),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {item.name for item in self.session.get_inputs()}
        self.max_len = _MAX_LEN_FALLBACK

    def embed_cls(self, text: str):  # type: ignore[no-untyped-def]
        np = self._np
        encoded = self.tokenizer.encode(text)
        ids = encoded.ids[: self.max_len]
        mask = [1] * len(ids)
        feed: dict[str, Any] = {
            "input_ids": np.asarray([ids], dtype="int64"),
            "attention_mask": np.asarray([mask], dtype="int64"),
        }
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros((1, len(ids)), dtype="int64")
        output = self.session.run(
            None, {key: value for key, value in feed.items() if key in self._input_names}
        )[0]
        vector = np.asarray(output)[0, 0]
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else vector


def _get_engine() -> _Engine | None:
    global _engine, _load_failed
    if _engine is not None:
        return _engine
    if _load_failed:
        return None
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _load_failed:
            return None
        model_dir = _model_dir()
        if model_dir is None:
            _load_failed = True
            return None
        try:
            _engine = _Engine(model_dir)
        except Exception as exc:  # noqa: BLE001 - optional local model must fail open
            logger.warning("local embedding model unavailable (%s); using lexical fallback", exc)
            _load_failed = True
            return None
        logger.info("local embedding model loaded from %s", model_dir)
        return _engine


def available() -> bool:
    return _get_engine() is not None


def embed(text: str) -> np.ndarray | None:
    if not text:
        return None
    with _cache_lock:
        cached = _cache.get(text, _MISS)
        if cached is not _MISS:
            _cache.move_to_end(text)
            return cached  # type: ignore[return-value]
    engine = _get_engine()
    vector: np.ndarray | None = None
    if engine is not None:
        try:
            vector = engine.embed_cls(text)
        except Exception:  # noqa: BLE001 - lexical fallback remains available
            vector = None
    with _cache_lock:
        _cache[text] = vector
        _cache.move_to_end(text)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return vector


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    return float((a * b).sum())


def similarity(a: str, b: str) -> float:
    return cosine(embed(a), embed(b))


def _reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
