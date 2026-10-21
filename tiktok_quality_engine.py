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
        raise RuntimeError("No video stream in " + path.name)
    video = videos[0]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = obj.get("format", {})
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for item in video.get("side_data_list", []):
        if "rotation" in item:
            rotation = int(float(item["rotation"])) % 360
    if rotation in (90, 270):
        width, height = height, width
    duration = number(video.get("duration")) or number(fmt.get("duration"))
    if not width or not height or not duration:
        raise RuntimeError("Missing dimensions/duration in " + path.name)
    sar = number(str(video.get("sample_aspect_ratio", "1/1")).replace(":", "/")) or 1
    aspect = width / height * (1 / sar if rotation in (90, 270) else sar)
    return {
        "width": width, "height": height,
        "fps": round(number(video.get("avg_frame_rate"))
                     or number(video.get("r_frame_rate")), 4),
        "codec": video.get("codec_name", "unknown"),
        # Deliberately NOT format.bit_rate: it includes audio and container overhead.
        "video_bitrate": int(number(video.get("bit_rate"))),
        "total_bitrate": int(number(fmt.get("bit_rate"))),
        "duration": round(duration, 4), "aspect": round(aspect, 5),
        "has_audio": bool(audios),
        "audio_channels": max((int(a.get("channels") or 0) for a in audios), default=0),
        "audio_codecs": sorted({a.get("codec_name", "unknown") for a in audios}),
        "audio_bitrate": sum(int(number(a.get("bit_rate"))) for a in audios),
        "color_transfer": video.get("color_transfer", "unknown"),
        "size": path.stat().st_size,
    }


def compare(old: dict[str, Any], new: dict[str, Any], ratio: float = 1.30) -> tuple[str, str]:
    """Classify evidence, not perceptual quality. Different codec bitrates are not ranked."""
    tolerance = min(2.0, max(0.5, old["duration"] * 0.01))
    if abs(old["duration"] - new["duration"]) > tolerance:
        return "reject", "duration mismatch (possible truncation or different edit)"
    if abs(new["aspect"] / old["aspect"] - 1) > 0.02:
        return "reject", "aspect ratio mismatch"
    if old["has_audio"] and not new["has_audio"]:
        return "reject", "candidate loses audio"
    if new["audio_channels"] < old["audio_channels"]:
        return "reject", "candidate has fewer audio channels"
    if (new["width"] < old["width"] * 0.98
            or new["height"] < old["height"] * 0.98):
        return "reject", "candidate has a lower image dimension"
    if old["fps"] and new["fps"] < old["fps"] * 0.95:
        return "reject", "candidate loses frame rate or has unknown FPS"
    old_hdr = old["color_transfer"] in {"smpte2084", "arib-std-b67"}
    new_hdr = new["color_transfer"] in {"smpte2084", "arib-std-b67"}
    if old_hdr != new_hdr:
        return "review", "different HDR/SDR encoding; no automatic quality judgment"
    if new["width"] * new["height"] > old["width"] * old["height"] * 1.10:
        return "resolution", "higher measured dimensions; visual quality not independently proven"
    if old["fps"] and new["fps"] > old["fps"] * 1.20:
        return "fps", "higher measured FPS; native extra motion detail not proven"
    if new["codec"] != old["codec"]:
        return "review", "different codec; bitrate is not directly comparable"
    if (old["video_bitrate"] > 0
            and new["video_bitrate"] >= old["video_bitrate"] * ratio):
        return "bitrate", "higher same-codec video bitrate; visual improvement not proven"
    return "keep", "no qualifying measured advantage"


def watermark_status(fmt: dict[str, Any]) -> str:
    fmtid = str(fmt.get("format_id", "")).lower()
    note = str(fmt.get("format_note", "")).lower()
    if "watermarked" in note or fmt.get("has_watermark") is True or fmtid == "download":
        return "flagged"
    if fmtid.startswith("download"):
        return "unknown"
    return "not_flagged"  # This does NOT mean visually certified watermark-free.


@dataclass
class Candidate:
    path: Path
    source: str
    watermark: str
    format_id: str
    info: dict[str, Any] = field(default_factory=dict)
    sha256: str = ""
    md5: str = ""
    kind: str = ""
    reason: str = ""


@dataclass
class Settings:
    watermark_policy: str = "separate"
    bitrate_policy: str = "separate"
    bitrate_ratio: float = 1.30
    max_formats: int = 6
    download_timeout: int = 300
    gallery: bool = True
    deadline: float = float("inf")

    def remaining(self, maximum: float) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Quality-check time budget exhausted")
        return min(maximum, remaining)


def ytdlp_base(impersonate: bool = False) -> list[str]:
    args = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
            "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2"]
    if impersonate:
        args += ["--impersonate", "chrome"]
    return args


def collect_candidates(url: str, tmp: Path, cfg: Settings,
                       record: dict[str, Any]) -> list[Candidate]:
    """Enumerate metadata, test exact download IDs separately, then playback variants.

    Exact repeated media URLs are skipped before downloading; byte duplicates are
    discarded by SHA-256 later. No assertion that two tools are independent sources.
    """
    available: list[tuple[dict[str, Any], Path, bool]] = []
    seen_urls: set[str] = set()
    manifest: list[dict[str, Any]] = []
    metadata_ok = 0
    warnings = record.setdefault("warnings", [])
    errors = record.setdefault("errors", [])
    for idx, impersonate in enumerate((False, True)):
        if cfg.deadline <= time.monotonic():
            errors.append("Time budget exhausted during format enumeration")
            break
        cookie_file = tmp / f"route{idx}.cookies.txt"
        p = command(ytdlp_base(impersonate) + ["--cookies", str(cookie_file),
                    "--dump-single-json", "--skip-download",
                    "--no-check-formats", url], cfg.remaining(75))
        if p.returncode:
            warnings.append(f"metadata route {idx}: {safe_error(p.stderr)}")
            continue
        try:
            meta = json.loads(p.stdout)
            expected = url.rsplit("/", 1)[-1]
            if str(meta.get("id")) != expected:
                raise ValueError("extractor returned a different video ID")
            metadata_ok += 1
            info_path = tmp / f"route{idx}.info.json"
            write_json(info_path, meta)  # Temporary only; never uploaded.
            for fmt in meta.get("formats", []):
                if fmt.get("vcodec") == "none" or not fmt.get("url"):
                    continue
                wm = watermark_status(fmt)
                entry = {"route": idx, "format_id": str(fmt.get("format_id", "")),
                         "width": fmt.get("width"), "height": fmt.get("height"),
                         "tbr": fmt.get("tbr"), "vcodec": fmt.get("vcodec"),
                         "watermark": wm}
                key = str(fmt["url"])
                if key in seen_urls:
                    entry["duplicate_url"] = True
                else:
                    seen_urls.add(key)
                    available.append((fmt, info_path, impersonate))
                manifest.append(entry)
        except (ValueError, TypeError) as exc:
            warnings.append(f"metadata route {idx}: {safe_error(exc)}")
    record["formats"] = manifest
    log("FORMATS", json.dumps(manifest, sort_keys=True))
    download_formats = [item for item in available
                        if str(item[0].get("format_id", "")).startswith("download")]
    playback = [item for item in available if item not in download_formats]
    # Sort reported quality, but inspect actual downloaded files before decisions.
    playback.sort(key=lambda item: (
        min(number(item[0].get("width")), number(item[0].get("height"))),
        number(item[0].get("fps")), number(item[0].get("vbr")) or number(item[0].get("tbr"))
    ), reverse=True)
    # Keep a fair chance for each codec before filling remaining slots.
    first, rest, codecs = [], [], set()
    for item in playback:
        codec = str(item[0].get("vcodec", "unknown"))
        (rest if codec in codecs else first).append(item)
        codecs.add(codec)
    playback = first + rest
    if cfg.max_formats and len(playback) > cfg.max_formats:
        warnings.append(f"Playback test cap: testing {cfg.max_formats} of {len(playback)} URL-distinct formats")
        playback = playback[:cfg.max_formats]
    # Critical v2 fix: unknown dimensions do NOT exclude the download source.
    selected = download_formats + playback
    result: list[Candidate] = []
    for index, (fmt, info_path, impersonate) in enumerate(selected):
        if cfg.deadline <= time.monotonic():
            errors.append("Time budget exhausted; not every selected format was tested")
            break
        fmtid = str(fmt["format_id"])
        wm = watermark_status(fmt)
        if wm != "not_flagged" and cfg.watermark_policy == "skip":
            log("POLICY SKIP", fmtid, "watermark=" + wm)
            continue
        dest = tmp / f"candidate{index}"
        dest.mkdir()
        # Exact format ID. Loading the captured metadata prevents a second page
        # extraction from silently changing which variant this ID refers to.
        selector = fmtid if fmt.get("acodec") != "none" else fmtid + "+ba/" + fmtid
        cookie_file = tmp / info_path.name.replace(".info.json", ".cookies.txt")
        args = ytdlp_base(impersonate) + ["--cookies", str(cookie_file),
                "--no-check-formats", "--format", selector,
                "--merge-output-format", "mp4", "--load-info-json", str(info_path),
                "--output", str(dest / "video.%(ext)s")]
        p = command(args, cfg.remaining(cfg.download_timeout))
        paths = [x for x in dest.iterdir() if x.suffix.lower() in VIDEO_EXT]
        if (p.returncode or not paths) and cfg.deadline > time.monotonic():
            # A signed URL or session can expire. Refresh once, requesting the
            # SAME explicit format, never silently falling back to best.
            refreshed = ytdlp_base(impersonate) + ["--cookies", str(cookie_file),
                "--no-check-formats", "--format", selector, "--merge-output-format", "mp4",
                "--output", str(dest / "video.%(ext)s"), url]
            p = command(refreshed, cfg.remaining(cfg.download_timeout))
            paths = [x for x in dest.iterdir() if x.suffix.lower() in VIDEO_EXT]
        if p.returncode or not paths:
            warnings.append(f"format {fmtid}: {safe_error(p.stderr) or 'no complete video'}")
            continue
        result.append(Candidate(paths[0], "yt-dlp", wm, fmtid))
    # If metadata extraction failed entirely, still try the exact download source.
    if not metadata_ok:
        for fmtid in ("download", "bv*+ba/b"):
            if cfg.deadline <= time.monotonic():
                break
            if fmtid == "download" and cfg.watermark_policy == "skip":
                continue
            dest = tmp / ("fallback_download" if fmtid == "download" else "fallback_best")
            dest.mkdir()
            p = command(ytdlp_base() + ["--format", fmtid, "--merge-output-format", "mp4",
                        "--output", str(dest / "video.%(ext)s"), url],
                        cfg.remaining(cfg.download_timeout))
            paths = [x for x in dest.iterdir() if x.suffix.lower() in VIDEO_EXT]
            if p.returncode or not paths:
                warnings.append(f"fallback {fmtid}: {safe_error(p.stderr)}")
            else:
                result.append(Candidate(paths[0], "yt-dlp-fallback",
                                        "flagged" if fmtid == "download" else "unknown", fmtid))
    if cfg.gallery and cfg.deadline > time.monotonic() and shutil.which("gallery-dl"):
        dest = tmp / "gallery"
        dest.mkdir()
        p = command(["gallery-dl", "--config-ignore", "-D", str(dest),
                     "-f", "source_{id}.{extension}", "-o", "tiktok.photos=false",
                     "-o", "tiktok.videos=true", "-o", "tiktok.audio=false",
                     "-o", "tiktok.covers=false", url], cfg.remaining(cfg.download_timeout))
        if p.returncode:
            warnings.append("gallery-dl: " + safe_error(p.stderr))
        else:
            for path in dest.rglob("*"):
                if path.is_file() and path.suffix.lower() in VIDEO_EXT:
                    result.append(Candidate(path, "gallery-dl", "unknown", "gallery"))
    if (download_formats and cfg.watermark_policy != "skip"
            and not any(c.format_id.startswith("download") for c in result)):
        errors.append("The listed download-format candidates all failed to download")
    if not result:
        errors.append("No candidate file downloaded; this is NOT a successful quality check")
    return result


def classify_candidates(local: Path, url: str, tmp: Path, cfg: Settings,
                        record: dict[str, Any]) -> tuple[Candidate | None, list[Candidate]]:
    old = probe(local)
    old_sha, old_md5 = hashes(local)
    record.update(baseline=old, baseline_sha256=old_sha, baseline_md5=old_md5,
                  candidates=[], duplicates=0)
    log("BASELINE", local.name, json.dumps(old, sort_keys=True))
    seen = {old_sha}
    eligible, alternatives = [], []
    successful_probes = 0
    for candidate in collect_candidates(url, tmp, cfg, record):
        item = {"source": candidate.source, "format_id": candidate.format_id,
                "watermark": candidate.watermark}
        try:
            candidate.info = probe(candidate.path)
            successful_probes += 1
            candidate.sha256, candidate.md5 = hashes(candidate.path)
            item.update(measured=candidate.info, sha256=candidate.sha256)
            log("CANDIDATE", candidate.format_id, "watermark=" + candidate.watermark,
                json.dumps(candidate.info, sort_keys=True))
            if candidate.sha256 in seen:
                record["duplicates"] += 1
                item.update(decision="duplicate", reason="identical SHA-256")
                log("DUPLICATE", candidate.format_id, "identical bytes")
                record["candidates"].append(item)
                continue
            seen.add(candidate.sha256)
            candidate.kind, candidate.reason = compare(old, candidate.info, cfg.bitrate_ratio)
            # Some download encodes have an added end card. Do not drop a
            # potentially useful file merely for a short added tail, and do not
            # promote it as though its runtime/content matched the original.
            extra = candidate.info["duration"] - old["duration"]
            if (candidate.kind == "reject" and candidate.reason.startswith("duration mismatch")
                    and candidate.watermark != "not_flagged" and 0 < extra <= 10):
                comparable = {**candidate.info, "duration": old["duration"]}
                other_kind, _ = compare(old, comparable, cfg.bitrate_ratio)
                if other_kind in {"resolution", "bitrate", "fps", "review"}:
                    candidate.kind = "review"
                    candidate.reason = (f"Candidate is {extra:.2f}s longer (possible end card); "
                                        "save separately, never automatically replace")
            item.update(decision=candidate.kind, reason=candidate.reason)
            if candidate.kind in {"keep", "reject"}:
                record["candidates"].append(item)
                continue
            can_replace = candidate.kind == "resolution" or (
                candidate.kind in {"bitrate", "fps"} and cfg.bitrate_policy == "allow")
            if candidate.watermark != "not_flagged":
                can_replace = can_replace and cfg.watermark_policy == "allow"
                if cfg.watermark_policy == "skip":
                    record["candidates"].append(item)
                    continue
            if candidate.path.suffix.lower() != local.suffix.lower():
                can_replace = False
                item["reason"] += "; different container extension: keep separately"
            (eligible if can_replace else alternatives).append(candidate)
            record["candidates"].append(item)
        except Exception as exc:
            record.setdefault("errors", []).append("Candidate inspection: " + safe_error(exc))
    if not successful_probes:
        record.setdefault("errors", []).append("No downloaded candidate could be inspected")
    # Resolution first. Within a codec this is a deterministic size/FPS/bitrate
    # preference, NOT a claim that bitrate proves quality across codecs.
    def rank(c: Candidate) -> tuple[float, ...]:
        return (min(c.info["width"], c.info["height"]), c.info["fps"],
                int(c.watermark == "not_flagged"),
                int(c.info["codec"] == old["codec"]), c.info["video_bitrate"])
    eligible.sort(key=rank, reverse=True)
    winner = eligible[0] if eligible else None
    alternatives.extend(eligible[1:])
    # Avoid saving every CDN mirror / encode. Retain strongest per evidence,
    # codec, and watermark class. Identical bytes were already excluded.
    best: dict[tuple[str, str, str], Candidate] = {}
    for candidate in alternatives:
        key = (candidate.kind, candidate.info["codec"], candidate.watermark)
        if key not in best or rank(candidate) > rank(best[key]):
            best[key] = candidate
    return winner, list(best.values())


def remote_stat(remote: str) -> dict[str, Any]:
    obj = json.loads(require(["rclone", "lsjson", remote, "--stat", "--hash"], 180))
    if not isinstance(obj, dict) or obj.get("IsDir"):
        raise RuntimeError("Expected one remote file: " + remote)
    return obj


def verify_remote(local: Path, remote: str, md5: str | None = None) -> None:
    obj = remote_stat(remote)
    remote_md5 = next((str(v).lower() for k, v in obj.get("Hashes", {}).items()
                       if k.lower().replace("-", "") == "md5"), "")
    expected_md5 = md5 or hashes(local)[1]
    if obj.get("Size") != local.stat().st_size or remote_md5 != expected_md5:
        raise RuntimeError("Remote size/MD5 verification failed (or MD5 unavailable): " + remote)


def upload_verified(local: Path, remote: str, immutable: bool = False) -> None:
    args = ["rclone", "copyto", str(local), remote, "--checksum", "--retries", "3"]
    if immutable:
        args.append("--immutable")
    require(args, 600)
    verify_remote(local, remote)


def safe_component(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:80]


def persist_alternative(candidate: Candidate, local: Path, folder: Path,
                        remote: str | None, vid: str) -> str:
    tag = ("watermark-" + candidate.watermark if candidate.watermark != "not_flagged"
           else "not-watermark-flagged")
    name = (f"{local.stem}__{safe_component(candidate.format_id)}__{tag}"
            f"__{candidate.sha256[:12]}{candidate.path.suffix.lower()}")
    relative = Path("_quality_alternatives") / vid / name
    target = folder / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate.path, target)
    metadata = target.with_suffix(target.suffix + ".json")
    write_json(metadata, {"engine_version": VERSION, "baseline_filename": local.name,
        "source": candidate.source, "format_id": candidate.format_id,
        "watermark_status": candidate.watermark,
        "watermark_method": "extractor metadata/source class; NOT optical detection",
        "sha256": candidate.sha256, "measured": candidate.info,
        "classification": candidate.kind, "reason": candidate.reason,
        "visual_improvement_verified": False})
    if remote:
        upload_verified(target, remote + "/" + relative.as_posix(), immutable=True)
        # Metadata is deterministic, so retries are idempotent.
        upload_verified(metadata, remote + "/" + relative.as_posix() + ".json")
        return remote + "/" + relative.as_posix()
    return str(target)


def replace_with_backup(candidate: Candidate, local: Path, folder: Path,
                        remote: str | None, vid: str, record: dict[str, Any]) -> str:
    backup_rel = Path("_quality_backups") / vid / (
        local.stem + "__" + record["baseline_sha256"][:16] + local.suffix)
    backup = folder / backup_rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, backup)
    if remote:
        original = remote + "/" + local.name
        # Refuse to replace a file that changed after we downloaded the baseline.
        verify_remote(local, original, record["baseline_md5"])
        upload_verified(backup, remote + "/" + backup_rel.as_posix(), immutable=True)
        record["backup_saved"] = remote + "/" + backup_rel.as_posix()
        upload_verified(candidate.path, original)
        backup_path = remote + "/" + backup_rel.as_posix()
    else:
        staging = local.with_name(local.name + ".quality.tmp")
        shutil.copy2(candidate.path, staging)
        os.replace(staging, local)
        backup_path = str(backup)
    record["replacement"] = {"source": candidate.source, "format_id": candidate.format_id,
        "watermark": candidate.watermark, "measured": candidate.info,
        "reason": candidate.reason, "sha256": candidate.sha256, "backup": backup_path}
    return backup_path


def process_file(username: str, local: Path, folder: Path,
                 cfg: Settings, remote: str | None = None) -> dict[str, Any]:
    match = VIDEO_ID.search(local.name)
    if not match:
        return {"filename": local.name, "status": "skipped", "errors": [], "warnings": [],
                "reason": "unrecognized video filename"}
    vid = match.group(1)
    record: dict[str, Any] = {"filename": local.name, "video_id": vid, "started": utcnow(),
                             "status": "kept", "errors": [], "warnings": [], "alternatives": []}
    try:
        with tempfile.TemporaryDirectory(prefix="tiktok-quality-") as td:
            winner, alternatives = classify_candidates(
                local, f"https://www.tiktok.com/@{username}/video/{vid}", Path(td), cfg, record)
            # Saving alternatives first means a failed optional copy cannot destroy
            # the current original. Every destructive replacement has a backup.
            for candidate in alternatives:
                try:
                    path = persist_alternative(candidate, local, folder, remote, vid)
                    record["alternatives"].append(path)
                    log("SAVED ALTERNATIVE", path)
                except Exception as exc:
                    record["errors"].append("Alternative upload: " + safe_error(exc))
            if winner:
                backup = replace_with_backup(winner, local, folder, remote, vid, record)
                record["status"] = "replaced"
                log("UPGRADED" if remote else "LOCAL REPLACEMENT", local.name,
                    "format=" + winner.format_id, "backup=" + backup)
            elif record["alternatives"]:
                record["status"] = "alternatives_saved"
                log("KEEP ORIGINAL; alternatives saved:", local.name)
            else:
                log("KEEP existing:", local.name)
    except Exception as exc:
        record["errors"].append(safe_error(exc))
    if record["errors"]:
        record["status"] = "partial_error" if record.get("replacement") or record["alternatives"] else "error"
    for message in record["errors"]:
        log("ERROR", local.name, message)
    record["finished"] = utcnow()
    return record


def load_state(remote: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    name = "quality_recheck_state.json"
    if not any(x.get("Name") == name for x in entries):
        return {"version": VERSION, "checked": {}, "checks_v2": {}}
    # A failed read is not treated as an empty state and overwritten.
    obj = json.loads(require(["rclone", "cat", remote + "/" + name], 180))
    if not isinstance(obj, dict) or not isinstance(obj.get("checks_v2", {}), dict):
        raise RuntimeError("Invalid repair state; refusing to overwrite it")
    obj.setdefault("checks_v2", {})
    obj["version"] = VERSION
    return obj


def choose_files(entries: list[dict[str, Any]], state: dict[str, Any],
                 limit: int, ids: set[str]) -> list[str]:
    names: list[str] = []
    for item in entries:
        name = item.get("Name", "")
        match = VIDEO_ID.search(name)
        if (not item.get("IsDir") and Path(name).name == name
                and Path(name).suffix.lower() in VIDEO_EXT and match
                and (not ids or match.group(1) in ids)):
            names.append(name)
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate filenames on Drive: refusing ambiguous replacement")
    if ids:
        found = {VIDEO_ID.search(name).group(1) for name in names}
        if ids - found:
            raise RuntimeError("Requested video IDs not found in Drive: " + ", ".join(sorted(ids - found)))
    checked = state.get("checks_v2", {})
    names.sort(key=lambda name: (checked.get(name, {}).get("attempted_at", ""), name))
    return names if limit == 0 else names[:limit]


def make_summary(report: dict[str, Any]) -> str:
    records = report["records"]
    replaced = sum(bool(r.get("replacement")) for r in records)
    alternatives = sum(len(r.get("alternatives", [])) for r in records)
    errors = sum(len(r.get("errors", [])) for r in records) + len(report.get("errors", []))
    duplicates = sum(r.get("duplicates", 0) for r in records)
    warnings = sum(len(r.get("warnings", [])) for r in records)
    report["counts"] = {"checked": len(records), "replaced": replaced, "alternatives_saved": alternatives,
                        "duplicates": duplicates, "errors": errors, "warnings": warnings}
    lines = [f"## TikTok quality v{VERSION}: @{report['username']}",
             f"Mode: **{report['mode']}**. Checked **{len(records)}**; replacements **{replaced}**; "
             f"alternatives saved **{alternatives}**; duplicates **{duplicates}**; errors **{errors}**.",
             "", "Higher bitrate alone does not prove better visual quality. "
             "Watermark classification uses extractor metadata, not image inspection.", ""]
    for rec in records:
        lines.append(f"- `{rec['filename']}`: **{rec['status']}**")
        for path in rec.get("alternatives", []):
            lines.append(f"  - Alternative: `{path}`")
        if rec.get("replacement"):
            lines.append(f"  - Backup: `{rec['replacement']['backup']}`")
        elif rec.get("backup_saved"):
            lines.append(f"  - Recovery backup: `{rec['backup_saved']}`")
        for err in rec.get("errors", []):
            lines.append("  - Error: " + err.replace("\n", " "))
    if report.get("stopped_early"):
        lines.append("\nStopped at the time budget. Unprocessed files remain for a later run.")
    if report["mode"] == "new":
        lines.append("\nNew-mode results are local; the main workflow uploads these files afterward.")
    return "\n".join(lines) + "\n"


def execute(args: argparse.Namespace) -> int:
    cfg = Settings(args.watermark_policy, args.bitrate_policy, args.bitrate_ratio,
                   args.max_formats, args.download_timeout, not args.no_gallery,
                   time.monotonic() + args.time_budget)
    report: dict[str, Any] = {"version": VERSION, "username": args.username, "mode": args.mode,
                             "started": utcnow(), "records": [], "errors": []}
    path = Path(args.report or f"quality_reports/{args.username}-{args.mode}.json")
    try:
        if args.mode == "new":
            folder = Path(args.folder)
            targets = sorted(p for p in folder.iterdir()
                             if p.is_file() and p.suffix.lower() in VIDEO_EXT and VIDEO_ID.search(p.name))
            for local in targets:
                if cfg.deadline <= time.monotonic():
                    report["stopped_early"] = True
                    break
                report["records"].append(process_file(args.username, local, folder, cfg))
                write_json(path, report)
        else:
            remote = args.remote_root.rstrip("/") + "/" + args.username
            entries = json.loads(require(["rclone", "lsjson", remote, "--files-only",
                                          "--max-depth", "1"], 180))
            state = load_state(remote, entries)
            ids = set(filter(None, re.split(r"[,\s]+", args.video_ids.strip())))
            files = choose_files(entries, state, args.limit, ids)
            log(f"{args.username}: selected {len(files)} video files; watermark={args.watermark_policy}; "
                f"bitrate={args.bitrate_policy}")
            for name in files:
                if cfg.deadline <= time.monotonic():
                    report["stopped_early"] = True
                    break
                with tempfile.TemporaryDirectory(prefix="tiktok-baseline-") as td:
                    folder = Path(td)
                    local = folder / name
                    try:
                        require(["rclone", "copyto", remote + "/" + name, str(local)], 600)
                        verify_remote(local, remote + "/" + name)
                        record = process_file(args.username, local, folder, cfg, remote)
                    except Exception as exc:
                        record = {"filename": name, "status": "error", "errors": [safe_error(exc)],
                                  "warnings": [], "alternatives": [], "finished": utcnow()}
                    report["records"].append(record)
                    # v2 state is separate: v1 checks never exclude the new download-format test.
                    state["checks_v2"][name] = {"attempted_at": utcnow(), "status": record["status"],
                                                "errors": len(record.get("errors", []))}
                    statefile = folder / "quality_recheck_state.json"
                    write_json(statefile, state)
                    upload_verified(statefile, remote + "/quality_recheck_state.json")
                    write_json(path, report)
    except Exception as exc:
        report["errors"].append(safe_error(exc))
        log("ERROR", safe_error(exc))
    report["finished"] = utcnow()
    summary = make_summary(report)
    write_json(path, report)
    path.with_suffix(".md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary)
    log("RESULT:", args.username, json.dumps(report["counts"], sort_keys=True))
    if report.get("stopped_early"):
        log("TIME BUDGET reached; not all selected videos were checked")
    return 1 if report["counts"]["errors"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["new", "repair"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--folder")
    parser.add_argument("--limit", type=int, default=5, help="0 = all, subject to time budget")
    parser.add_argument("--video-ids", default="", help="Optional comma/space separated IDs for repair")
    parser.add_argument("--remote-root", default="gdrive:TikTok")
    parser.add_argument("--watermark-policy", choices=["separate", "allow", "skip"], default="separate")
    parser.add_argument("--bitrate-policy", choices=["separate", "allow"], default="separate")
    parser.add_argument("--bitrate-ratio", type=float, default=1.30)
    parser.add_argument("--max-formats", type=int, default=6, help="Playback variants; 0=all. Download formats are extra.")
    parser.add_argument("--download-timeout", type=int, default=300)
    parser.add_argument("--time-budget", type=int, default=2400, help="Seconds for candidate checks; persistence may take longer")
    parser.add_argument("--no-gallery", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    args.username = args.username.lstrip("@")
    if not USERNAME_RE.fullmatch(args.username) or args.username in {".", ".."}:
        parser.error("Invalid username")
    if args.mode == "new" and not args.folder:
        parser.error("--folder is required for new mode")
    if (args.limit < 0 or args.max_formats < 0 or args.download_timeout < 1
            or args.time_budget < 1 or not math.isfinite(args.bitrate_ratio) or args.bitrate_ratio <= 1):
        parser.error("Invalid limits, timeout, or bitrate ratio")
    ids = set(filter(None, re.split(r"[,\s]+", args.video_ids.strip())))
    if any(not re.fullmatch(r"\d{17,21}", vid) for vid in ids):
        parser.error("Video IDs must be 17-21 digits, separated by commas or spaces")
    return args


if __name__ == "__main__":
    sys.exit(execute(parse_args()))
