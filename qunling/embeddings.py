"""Pinned CPU Chinese embeddings shared by online recall and offline reindexing.

The model name is an actual implementation choice, not a display label. Keep
legacy MiniLM available until its collections have been migrated offline.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import urllib.request

MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "bge-small-zh-v1.5-int8-cls-chunks-v1"
DIMENSION = 512
SOURCE = "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b069aac4d136ba6bc1122f995fedcfd2781/"
ASSETS = {
    "tokenizer.json": ("tokenizer.json", 439125,
                       "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    "model_quantized.onnx": ("onnx/model_quantized.onnx", 24010842,
                            "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
}


def cache_dir() -> Path:
    return Path(os.environ.get("QUNLING_EMBEDDING_CACHE", Path.home() / ".cache/qunling/embeddings")) / REVISION


def _valid(path: Path, size: int, checksum: str) -> bool:
    return (path.is_file() and path.stat().st_size == size
            and hashlib.sha256(path.read_bytes()).hexdigest() == checksum)


def prepare(directory: Path | None = None) -> Path:
    """Download immutable, checksummed artifacts; never trust a partial cache."""
    directory = directory or cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for filename, (source, size, checksum) in ASSETS.items():
        destination = directory / filename
        if _valid(destination, size, checksum):
            continue
        descriptor, name = tempfile.mkstemp(prefix=filename + ".", dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                request = urllib.request.Request(SOURCE + source, headers={"User-Agent": "QunLing-embedding/1"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    remaining = size + 1
                    while remaining:
                        chunk = response.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        stream.write(chunk)
                        remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not _valid(temporary, size, checksum):
                raise ValueError("Chinese embedding artifact size or SHA-256 mismatch")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return directory


class ChineseEmbedding:
    """Normalized CLS, with bounded batches and complete long-memory coverage.

    BGE v1.5 supports symmetric short-text matching without a query prefix.
    Both migration and requests use exactly this pooling/chunking algorithm.
    """
    def __init__(self, directory: Path | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        directory = prepare(directory)
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls = self.tokenizer.token_to_id("[CLS]")
        self.sep = self.tokenizer.token_to_id("[SEP]")
        self.pad = self.tokenizer.token_to_id("[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model_quantized.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])
        self.inputs = {item.name for item in self.session.get_inputs()}
        self.lock = threading.Lock()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        if not texts:
            return []
        if len(texts) > 256 or any(not isinstance(text, str) or len(text) > 32000 for text in texts):
            raise ValueError("Embedding input exceeds the bounded text batch")
        with self.lock:
            chunks, owners, weights = [], [], []
            for owner, text in enumerate(texts):
                ids = self.tokenizer.encode(text, add_special_tokens=False).ids
                for start in range(0, max(1, len(ids)), 510):
                    part = ids[start:start + 510]
                    chunks.append([self.cls, *part, self.sep])
                    owners.append(owner)
                    weights.append(max(1, len(part)))
            output = np.zeros((len(texts), DIMENSION), dtype=np.float32)
            for start in range(0, len(chunks), 16):
                batch = chunks[start:start + 16]
                width = max(map(len, batch))
                ids = np.full((len(batch), width), self.pad, dtype=np.int64)
                mask = np.zeros_like(ids)
                for index, part in enumerate(batch):
                    ids[index, :len(part)] = part
                    mask[index, :len(part)] = 1
                inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
                states = self.session.run(None, {key: value for key, value in inputs.items() if key in self.inputs})[0]
                vectors = states[:, 0, :].astype(np.float32)
                if vectors.shape[1] != DIMENSION or not np.isfinite(vectors).all():
                    raise ValueError("Unexpected Chinese embedding output")
                vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
                for index, vector in enumerate(vectors, start):
                    output[owners[index]] += vector * weights[index]
            output /= np.maximum(np.linalg.norm(output, axis=1, keepdims=True), 1e-12)
            return output.tolist()


_instances = {}
_instance_lock = threading.Lock()


def embed(model_name: str, texts: list[str]) -> list[list[float]]:
    """Model dispatch also keeps old indexes readable during staged upgrades."""
    if model_name not in {MODEL, "bge-small-zh-v1.5", "all-MiniLM-L6-v2"}:
        raise ValueError("Unsupported local embedding model; select BAAI/bge-small-zh-v1.5")
    with _instance_lock:
        if model_name not in _instances:
            if model_name == "all-MiniLM-L6-v2":
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                _instances[model_name] = DefaultEmbeddingFunction()
            else:
                _instances[model_name] = ChineseEmbedding()
        implementation = _instances[model_name]
    result = implementation(texts)
    return [item.tolist() if hasattr(item, "tolist") else list(item) for item in result]
