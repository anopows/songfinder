"""On-disk store of reference features, keyed by source URL.

Only features and metadata are stored, never audio. A URL fetched once is
never fetched again, and the store doubles as a growing private index.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

import numpy as np

from .features import FEATURE_VERSION


class FeatureCache:
    def __init__(self, root: str | None):
        self.root = root
        if root:
            os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        h = hashlib.sha1(key.encode()).hexdigest()
        return os.path.join(self.root, h[:2], h + ".npz")

    def get(self, key: str):
        if not self.root:
            return None
        path = self._path(key)
        if not os.path.exists(path):
            return None
        try:
            with np.load(path, allow_pickle=False) as z:
                meta = json.loads(str(z["meta"]))
                if meta.get("version") != FEATURE_VERSION:
                    return None
                return z["f"].astype(np.float32), meta
        except Exception:
            return None                      # unreadable entry: treat as a miss

    def entries(self):
        """Metadata of every stored recording: the private index."""
        if not self.root:
            return
        for folder, _, files in os.walk(self.root):
            for name in files:
                if not name.endswith(".npz"):
                    continue
                try:
                    with np.load(os.path.join(folder, name), allow_pickle=False) as z:
                        meta = json.loads(str(z["meta"]))
                except Exception:
                    continue
                if meta.get("version") == FEATURE_VERSION and meta.get("key"):
                    yield meta

    def put(self, key: str, feats: np.ndarray, meta: dict) -> None:
        if not self.root:
            return
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        meta = dict(meta, version=FEATURE_VERSION, key=key)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        os.close(fd)
        try:
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, f=feats.astype(np.float16), meta=np.array(json.dumps(meta)))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
