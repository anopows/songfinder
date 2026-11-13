"""Decode any local file or http(s) stream to mono float32 PCM through FFmpeg.

Remote audio is piped straight from FFmpeg into memory. Nothing is written to
disk, which is what keeps reference fetching transient.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

SR = 22050
USER_AGENT = "songfinder/0.1 (local music matching tool)"


class DecodeError(RuntimeError):
    pass


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def decode(source: str | bytes, sr: int = SR, max_seconds: float | None = None,
           timeout: float = 240) -> np.ndarray:
    """Return mono float32 samples at `sr` for a file path, http(s) URL or encoded bytes."""
    data = None
    if isinstance(source, (bytes, bytearray)):
        data, src = bytes(source), "pipe:0"
        cmd = ["ffmpeg", "-v", "error"]
    else:
        src = str(source)
        cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if data is not None:
        pass
    elif is_url(src):
        # Only network protocols: a hostile playlist must not reach local files.
        cmd += ["-user_agent", USER_AGENT,
                "-protocol_whitelist", "http,https,tls,tcp,crypto",
                "-rw_timeout", "20000000"]
    elif not os.path.isfile(src):
        raise DecodeError(f"No such file: {src}")
    cmd += ["-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sr)]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-f", "f32le", "pipe:1"]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DecodeError("FFmpeg is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise DecodeError(f"FFmpeg timed out after {timeout:.0f}s") from e
    y = np.frombuffer(p.stdout, dtype="<f4")
    if y.size < sr:  # under one second of audio is unusable
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DecodeError(msg[-1] if msg else "No audio decoded")
    y = np.nan_to_num(y, copy=True)
    return y
