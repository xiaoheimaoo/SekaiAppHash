#!/usr/bin/env python3
"""Minimal Project SEKAI JP AppHash updater.

Files in the repository:
  .github/workflows/update.yml
  version.json
  update.py

The check stage uses only Python's standard library. UnityPy is imported only
for the update stage, after a new JP version has been detected.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath

# ------------------------------
# Project SEKAI JP constants
# ------------------------------
BUNDLE_ID = "com.sega.pjsekai"
ITUNES_LOOKUP = "https://itunes.apple.com/jp/lookup"
APKPURE_XAPK = "https://d.apkpure.com/b/XAPK/com.sega.pjsekai?version=latest"
TARGET_FILE = "6350e2ec327334c8a9b7f494f344a761"
OBJECT_NAME = "production_android"
UNITY_VERSION = "2022.3.62f2"
STATE_FILE = Path("version.json")

FIELDS = (
    "memo",
    "clientMajorVersion",
    "clientMinorVersion",
    "clientBuildVersion",
    "snapshot",
    "clientVersionSuffix",
    "clientDataMajorVersion",
    "clientDataMinorVersion",
    "clientDataBuildVersion",
    "clientDataRevision",
    "companyName",
    "productName",
    "bundleIdentifier",
    "bundleVersion",
    "assetHash",
    "clientAppHash",
)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def log(*values: object) -> None:
    print(*values, flush=True)


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("version.json 必须是 JSON object")
    return data


def write_state(data: dict) -> None:
    """Atomically replace version.json only after successful extraction."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------
# Version check (stdlib only)
# ------------------------------
def lookup_jp_version() -> str:
    query = urllib.parse.urlencode({
        "bundleId": BUNDLE_ID,
        "time": int(time.time()),  # cache buster
    })
    request = urllib.request.Request(
        f"{ITUNES_LOOKUP}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "SekaiAppHashUpdater/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    matches = [
        item for item in payload.get("results", [])
        if item.get("bundleId") == BUNDLE_ID
    ]
    if not matches:
        raise RuntimeError("iTunes JP lookup 没有返回 Project SEKAI")

    version = str(matches[0].get("version") or "").strip()
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"iTunes 返回的版本号无效: {version!r}")
    return version


def write_github_output(needs_update: bool, remote_version: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"needs_update={str(needs_update).lower()}",
        f"remote_version={remote_version}",
    ]
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    else:
        for line in lines:
            log(line)


def command_check(force: bool) -> None:
    remote = lookup_jp_version()
    saved = read_state()
    saved_version = str(saved.get("appVersion") or "").strip()
    saved_hash = str(saved.get("appHash") or "").strip()
    needs_update = force or not UUID_RE.fullmatch(saved_hash) or remote != saved_version

    log(f"[CHECK] iTunes JP version: {remote}")
    log(f"[CHECK] saved version: {saved_version or 'none'}")
    log(f"[CHECK] saved hash valid: {bool(UUID_RE.fullmatch(saved_hash))}")
    log(f"[CHECK] needs update: {needs_update}")
    write_github_output(needs_update, remote)


# ------------------------------
# XAPK download (stdlib only)
# ------------------------------
def download_xapk(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        APKPURE_XAPK,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://apkpure.com/",
            "Accept": "application/octet-stream,*/*",
            "Accept-Encoding": "identity",
        },
    )

    try:
        log("[DOWNLOAD]", APKPURE_XAPK)
        with urllib.request.urlopen(request, timeout=180) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise RuntimeError(
                    "APKPure 返回 HTML/验证页面，不是 XAPK；本次不会修改 version.json"
                )
            with partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

        if not zipfile.is_zipfile(partial):
            raise RuntimeError("下载结果不是有效 ZIP/XAPK")
        with zipfile.ZipFile(partial) as archive:
            if not any(
                name.lower().endswith(".apk")
                for name in archive.namelist()
            ):
                raise RuntimeError("下载的 ZIP 中没有 APK")

        os.replace(partial, destination)
        log(f"[DOWNLOADED] {destination.stat().st_size:,} bytes")
    finally:
        partial.unlink(missing_ok=True)


# ------------------------------
# Exact ZIP member lookup
# ------------------------------
def member_basename(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name.lower()


def locate_target(archive: zipfile.ZipFile):
    # Search the ZIP central directory only; do not scan file contents.
    for info in archive.infolist():
        if not info.is_dir() and member_basename(info.filename) == TARGET_FILE:
            return info
    return None


def apk_priority(info: zipfile.ZipInfo) -> tuple[int, str]:
    name = member_basename(info.filename)
    if name == "unitydataassetpack.apk":
        return 0, name
    if "asset" in name:
        return 1, name
    if name in ("base.apk", "com.sega.pjsekai.apk"):
        return 2, name
    if name.startswith(("config.", "config_", "split_config")):
        return 9, name
    return 3, name


def extract_target_bytes(package: Path) -> tuple[bytes, str]:
    with zipfile.ZipFile(package) as outer:
        direct = locate_target(outer)
        if direct:
            return outer.read(direct), direct.filename

        if package.suffix.lower() == ".apk":
            raise FileNotFoundError(f"APK 中没有目标文件 {TARGET_FILE}")

        apk_members = sorted(
            (
                item for item in outer.infolist()
                if not item.is_dir() and item.filename.lower().endswith(".apk")
            ),
            key=apk_priority,
        )

        if not apk_members:
            raise FileNotFoundError("XAPK 中没有内嵌 APK")

        for apk_info in apk_members:
            if apk_priority(apk_info)[0] == 9:
                continue

            log("[INDEX]", apk_info.filename)
            # Nested ZIP needs seek support; only spool the current APK.
            with tempfile.TemporaryFile(mode="w+b") as spool:
                with outer.open(apk_info) as source:
                    shutil.copyfileobj(source, spool, length=1024 * 1024)
                spool.seek(0)
                try:
                    with zipfile.ZipFile(spool) as inner:
                        target = locate_target(inner)
                        if target:
                            source_name = f"{apk_info.filename}!/{target.filename}"
                            log("[FOUND]", source_name)
                            return inner.read(target), source_name
                except zipfile.BadZipFile:
                    log("[SKIP] invalid nested APK:", apk_info.filename)

    raise FileNotFoundError(
        f"XAPK 中没有找到 {TARGET_FILE}；未修改旧版 version.json"
    )


# ------------------------------
# Unity SerializedFile / SEKAI header
# ------------------------------
def normalize_unity_file(raw: bytes) -> bytes:
    # SEKAI custom header marker: restore only when this exact marker exists.
    if raw.startswith(b"\x10\x00\x00\x00"):
        if len(raw) < 132:
            raise ValueError("SEKAI 加扰文件长度不足 132 字节")
        header = bytearray(raw[4:132])
        for block in range(0, 128, 8):
            for index in range(5):
                header[block + index] ^= 0xFF
        restored = bytes(header) + raw[132:]
        if not restored.startswith((b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0")):
            raise ValueError("SEKAI 头部恢复后不是 Unity Bundle")
        return restored

    if raw.startswith((b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0")):
        return raw

    # The known 500-byte production file is Unity SerializedFile v22.
    if len(raw) >= 16:
        version = int.from_bytes(raw[8:12], "big")
        if 5 <= version <= 30:
            log(f"[FORMAT] Unity SerializedFile v{version}")
            return raw

    raise ValueError(f"未知 Unity 文件头: {raw[:16].hex()}")


def read_unity_string(raw: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(raw):
        raise ValueError("Unity 字符串长度越界")
    length = struct.unpack_from("<I", raw, offset)[0]
    offset += 4
    if length > 8192 or offset + length > len(raw):
        raise ValueError(f"Unity 字符串长度无效: {length}")
    value = raw[offset:offset + length].decode("utf-8")
    offset = (offset + length + 3) & ~3
    return value, offset


def parse_raw_mono(raw: bytes) -> dict | None:
    marker = struct.pack("<I", len(OBJECT_NAME)) + OBJECT_NAME.encode("ascii")
    position = raw.find(marker, 0, min(len(raw), 80))
    if position < 0:
        return None

    position = (position + len(marker) + 3) & ~3
    values = {}
    for field in FIELDS:
        values[field], position = read_unity_string(raw, position)

    if not UUID_RE.fullmatch(str(values.get("clientAppHash", "")).strip()):
        raise ValueError("production_android 的 clientAppHash 校验失败")
    return values


def parse_identity(raw: bytes) -> dict:
    # Import UnityPy only in the update job.
    import UnityPy

    if hasattr(UnityPy.config, "FALLBACK_UNITY_VERSION"):
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_VERSION

    data = normalize_unity_file(raw)
    log(f"[PARSE] target size: {len(data):,} bytes")
    env = UnityPy.load(BytesIO(data))
    log(f"[UNITY] objects: {len(env.objects)}")

    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue

        try:
            mono = obj.read(check_read=False)
        except Exception:
            continue

        if getattr(mono, "m_Name", "") != OBJECT_NAME:
            continue

        log("[FOUND] MonoBehaviour: production_android")
        values = None

        # Prefer UnityPy's TypeTree when available.
        try:
            tree = obj.read_typetree()
            if isinstance(tree, dict) and UUID_RE.fullmatch(
                str(tree.get("clientAppHash", "")).strip()
            ):
                values = tree
        except Exception:
            pass

        # The known v22 / 500-byte file uses the verified fixed string layout.
        if values is None:
            chunk = obj.get_raw_data()
            raw_object = chunk.tobytes() if hasattr(chunk, "tobytes") else bytes(chunk)
            values = parse_raw_mono(raw_object)

        if not values:
            raise RuntimeError(
                "找到 production_android，但无法解析 clientAppHash 字段"
            )

        parts = [
            str(values.get(key, "")).strip()
            for key in (
                "clientMajorVersion",
                "clientMinorVersion",
                "clientBuildVersion",
            )
        ]
        version = ".".join(parts)
        if not VERSION_RE.fullmatch(version):
            version = str(values.get("bundleVersion", "")).strip()
        if not VERSION_RE.fullmatch(version):
            raise ValueError(f"客户端版本号无效: {version!r}")

        app_hash = str(values.get("clientAppHash", "")).strip()
        if not UUID_RE.fullmatch(app_hash):
            raise ValueError(f"clientAppHash 无效: {app_hash!r}")

        package = str(values.get("bundleIdentifier", "")).strip()
        if package and package != BUNDLE_ID:
            raise ValueError(f"检测到非日服客户端: {package}")

        return {
            "appVersion": version,
            "appHash": app_hash,
            "bundleIdentifier": package or BUNDLE_ID,
        }

    raise RuntimeError("SerializedFile 中没有 production_android MonoBehaviour")


# ------------------------------
# Update / publish state
# ------------------------------
def command_update(expected_version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="sekai-apphash-") as temp_dir:
        xapk = Path(temp_dir) / "sekai-jp.xapk"
        download_xapk(xapk)
        raw, source = extract_target_bytes(xapk)
        log("[TARGET]", source, f"({len(raw):,} bytes)")
        identity = parse_identity(raw)

    actual_version = identity["appVersion"]
    actual_hash = identity["appHash"]

    if expected_version and actual_version != expected_version:
        raise RuntimeError(
            f"Android XAPK 版本 {actual_version} 与 iTunes JP 版本 "
            f"{expected_version} 不一致；APKPure 可能尚未同步。"
            "保留旧 version.json，等待下次检查。"
        )

    old = read_state()
    if (
        old.get("appVersion") == actual_version
        and old.get("appHash") == actual_hash
    ):
        log("[NO CHANGE] App Version / App Hash 均未变化")
        return

    new_state = {
        "appVersion": actual_version,
        "appHash": actual_hash,
    }
    write_state(new_state)
    log("[UPDATED] version.json")
    log("[OK] X-App-Version:", actual_version)
    log("[OK] X-App-Hash:", actual_hash)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Project SEKAI JP version and update clientAppHash"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Check iTunes JP version")
    check_parser.add_argument("--force", action="store_true")

    update_parser = subparsers.add_parser("update", help="Download XAPK and extract AppHash")
    update_parser.add_argument("--expected-version", default="")

    args = parser.parse_args()
    if args.command == "check":
        command_check(args.force)
    elif args.command == "update":
        command_update(args.expected_version.strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"[ERROR] {type(error).__name__}: {error}")
        sys.exit(1)
