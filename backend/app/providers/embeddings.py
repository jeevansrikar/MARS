"""Pinned local ONNX embeddings. User text never leaves the worker for embedding."""

import argparse
import asyncio
import hashlib
import json
import threading
from functools import lru_cache

import numpy as np
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.errors import AppError

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REPO = "qdrant/all-MiniLM-L6-v2-onnx"
_inference_lock = threading.Lock()


@lru_cache(maxsize=1)
def load_model(cache_dir: str, revision: str):
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        MODEL_REPO,
        revision=revision,
        cache_dir=cache_dir,
        local_files_only=True,
        allow_patterns=["*.json", "model.onnx", "vocab.txt"],
        token=False,
    )
    return TextEmbedding(
        MODEL_NAME,
        specific_model_path=path,
        cache_dir=cache_dir,
        threads=2,
        providers=["CPUExecutionProvider"],
        local_files_only=True,
    )


def encode_local(texts: list[str], settings: Settings) -> list[list[float]]:
    try:
        with _inference_lock:
            model = load_model(settings.embedding_cache_dir, settings.embedding_revision)
            vectors = np.asarray(list(model.embed(texts, batch_size=16)), dtype=np.float32)
        if vectors.shape != (len(texts), 384) or not np.isfinite(vectors).all():
            raise ValueError("Invalid embedding shape")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError("Empty embedding")
        return (vectors / norms).tolist()
    except Exception as exc:
        raise AppError(
            "EMBEDDING_UNAVAILABLE", "Local embedding model unavailable; provision its pinned assets", 503
        ) from exc


class Embedder:
    def __init__(self, settings: Settings, redis, owner: str):
        self.settings, self.redis, self.owner = settings, redis, owner
        self.model_id = f"{MODEL_NAME}@{settings.embedding_revision}"

    async def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 128 or any(len(t) > 2000 for t in texts):
            raise AppError("EMBEDDING_BUDGET", "Embedding batch exceeds its resource bound")
        keys = [
            f"embedding:{self.owner}:" + hashlib.sha256((self.model_id + ":" + text).encode()).hexdigest()
            for text in texts
        ]
        try:
            cached = await self.redis.mget(keys)
        except RedisError:
            cached = [None] * len(texts)
        output: list[list[float] | None] = []
        for value in cached:
            try:
                vector = json.loads(value) if value else None
                output.append(vector if isinstance(vector, list) and len(vector) == 384 else None)
            except (TypeError, ValueError):
                output.append(None)
        missing = [index for index, value in enumerate(output) if value is None]
        if missing:
            vectors = await asyncio.to_thread(encode_local, [texts[i] for i in missing], self.settings)
            for index, vector in zip(missing, vectors, strict=True):
                output[index] = vector
                try:
                    await self.redis.set(keys[index], json.dumps(vector), ex=86400)
                except RedisError:
                    continue
        return [vector for vector in output if vector is not None]

    async def rank(self, query: str, passages: dict[str, dict], limit: int = 20) -> list[str]:
        ids = list(passages)[:64]
        vectors = np.asarray(await self.encode([query] + [passages[key]["passage"][:1500] for key in ids]))
        scores = vectors[1:] @ vectors[0]
        return [ids[int(i)] for i in np.argsort(-scores, kind="stable")[:limit]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download", action="store_true", help="Provision pinned public model assets before deployment"
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.download:
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODEL_REPO,
            revision=settings.embedding_revision,
            cache_dir=settings.embedding_cache_dir,
            allow_patterns=["*.json", "model.onnx", "vocab.txt"],
            token=False,
        )
    encode_local(["Embedding readiness check"], settings)
    print("Pinned local embedding model ready (384 dimensions).")


if __name__ == "__main__":
    main()
