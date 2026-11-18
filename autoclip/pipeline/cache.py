"""Cache disque des étapes du pipeline : un dossier par étape, écritures atomiques."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, step: str, name: str) -> Path:
        directory = self.root / step
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    @staticmethod
    def write_json(path: Path, data: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, path)

    @staticmethod
    def read_json(path: Path) -> Any:
        return json.loads(path.read_text())


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def short_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]
