"""Configuration loading: JSON file + environment variable overrides.

Priority (highest wins):
  1. Environment variables: ONEC_CONFIG_ROOT, ONEC_QDRANT_URL, ONEC_EMBEDDER_API_KEY, ...
  2. JSON config file (path from ONEC_MCP_CONFIG env var, default ./config.json).

For production deployments put secrets (api keys) in env vars, not in the JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass
class EmbedderConfig:
    base_url: str = ""  # OpenAI-compatible, e.g. http://localhost:11434/v1
    api_key: str = ""
    model: str = "bge-m3"
    dimensions: int = 1024
    batch_size: int = 32
    max_text_chars: int = 8000


@dataclass
class RerankerConfig:
    endpoint: str = ""  # e.g. http://localhost:8080/rerank (Cohere/Jina-compatible)
    api_key: str = ""
    model: str = ""
    top_n: int = 10
    timeout: float = 30.0


@dataclass
class SearchConfig:
    top_k: int = 5
    candidate_multiplier: int = 4  # fetch top_k * multiplier candidates, then rerank


@dataclass
class AppConfig:
    config_root: Path = Path(".")  # root of the 1C configuration sources (XML/BS export)
    project_data_dir: Path = Path("./data/AKADA")  # project export directory (metadata/, code/)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "onec_config"
    graph_db_path: Path = Path("./data/graph.sqlite3")
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    host: str = "0.0.0.0"
    port: int = 8765

    def validate(self) -> None:
        if not self.config_root.is_dir():
            raise ConfigError(
                f"config_root does not exist or is not a directory: {self.config_root}"
            )
        if not self.qdrant_url:
            raise ConfigError("qdrant.url is required")
        if not self.embedder.base_url:
            raise ConfigError("embedder.base_url is required (OpenAI-compatible /embeddings)")


# env var -> (attribute path on AppConfig)
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "ONEC_CONFIG_ROOT": ("config_root",),
    "ONEC_PROJECT_DATA_DIR": ("project_data_dir",),
    "ONEC_QDRANT_URL": ("qdrant_url",),
    "ONEC_QDRANT_API_KEY": ("qdrant_api_key",),
    "ONEC_QDRANT_COLLECTION": ("qdrant_collection",),
    "ONEC_GRAPH_DB_PATH": ("graph_db_path",),
    "ONEC_EMBEDDER_BASE_URL": ("embedder", "base_url"),
    "ONEC_EMBEDDER_API_KEY": ("embedder", "api_key"),
    "ONEC_EMBEDDER_MODEL": ("embedder", "model"),
    "ONEC_EMBEDDER_DIMENSIONS": ("embedder", "dimensions"),
    "ONEC_EMBEDDER_BATCH_SIZE": ("embedder", "batch_size"),
    "ONEC_RERANKER_ENDPOINT": ("reranker", "endpoint"),
    "ONEC_RERANKER_API_KEY": ("reranker", "api_key"),
    "ONEC_RERANKER_MODEL": ("reranker", "model"),
    "ONEC_SEARCH_TOP_K": ("search", "top_k"),
    "ONEC_HOST": ("host",),
    "ONEC_PORT": ("port",),
}


def _coerce(value: str, target_type: type) -> Any:
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if isinstance(target_type, type) and issubclass(target_type, Path):
        return Path(value).expanduser()
    return value


def _set_nested(cfg: AppConfig, path: tuple[str, ...], raw: str) -> None:
    obj: Any = cfg
    for key in path[:-1]:
        obj = getattr(obj, key)
    leaf = path[-1]
    setattr(obj, leaf, _coerce(raw, type(getattr(obj, leaf))))


def load_config(config_path: str | os.PathLike | None = None) -> AppConfig:
    """Load config from JSON file (if present) and apply env var overrides."""
    cfg = AppConfig()

    path = Path(
        config_path or os.environ.get("ONEC_MCP_CONFIG", "./config.json")
    ).expanduser()

    if path.is_file():
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        _apply_json(cfg, data)
    elif config_path is not None:
        raise ConfigError(f"config file not found: {path}")

    for env_name, path_tuple in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            _set_nested(cfg, path_tuple, raw)

    cfg.validate()
    return cfg


def _apply_json(cfg: AppConfig, data: dict[str, Any]) -> None:
    def put(obj: Any, key: str) -> None:
        if key in data:
            setattr(obj, key, _coerce(data[key], type(getattr(obj, key))))

    for key in ("config_root", "project_data_dir", "qdrant_url", "qdrant_api_key",
                "qdrant_collection", "graph_db_path", "host", "port"):
        put(cfg, key)

    if isinstance(data.get("qdrant"), dict):
        q = data["qdrant"]
        for k in ("url", "api_key", "collection"):
            attr = f"qdrant_{k}"
            if k in q:
                setattr(cfg, attr, _coerce(q[k], type(getattr(cfg, attr))))

    for section in ("embedder", "reranker", "search"):
        if isinstance(data.get(section), dict):
            obj = getattr(cfg, section)
            for k, v in data[section].items():
                if hasattr(obj, k):
                    setattr(obj, k, _coerce(v, type(getattr(obj, k))))
