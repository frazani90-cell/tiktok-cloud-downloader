#!/usr/bin/env python3
"""TikTok quality engine v2: measure source variants; never upscale or re-encode.

Default policy: higher-resolution, unflagged candidates may replace the baseline,
with a verified backup. Same-resolution bitrate/FPS candidates, codec alternatives,
and watermark-flagged/unknown candidates are kept separately for review.

Requires Python 3.10+, yt-dlp, ffprobe, rclone. gallery-dl is optional.
No claim of app-only access, optical watermark detection, or perceptual quality.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

VERSION = "2.0.0"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
VIDEO_ID = re.compile(r"(?:^|_)(\d{17,21})(?=_|\.|$)")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.]{1,64}\Z")


def number(value: Any) -> float:
    try:
        val = float(Fraction(str(value)))
        return val if math.isfinite(val) and val >= 0 else 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_error(text: Any) -> str:
    # Do not expose signed media URLs/cookies in logs or reports.
    return re.sub(r"https?://\S+", "[URL omitted]", str(text))[-1200:]


def log(*args: Any) -> None:
    print(*args, flush=True)


def command(cmd: list[str], timeout: float = 180) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]} timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def require(cmd: list[str], timeout: float = 180) -> str:
    result = command(cmd, timeout)
    if result.returncode:
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {safe_error(result.stderr)}")
    return result.stdout


def hashes(path: Path) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def probe(path: Path) -> dict[str, Any]:
    obj = json.loads(require(["ffprobe", "-v", "error", "-show_streams",
                              "-show_format", "-of", "json", str(path)], 60))
    streams = obj.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
