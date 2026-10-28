"""Windows image (WIM/ESD) helpers.

Reading the image list/version needs nothing: the XML index of a WIM is stored
uncompressed, so we read it straight out of the ISO. Extracting files from a
WIM (only needed for the 'Windows CA 2023' option) uses wimlib or 7-Zip.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET

from .isofs import Node

ARCH_NAMES = {0: "x86", 5: "arm", 9: "amd64", 12: "arm64"}
FEATURE_UPDATES = {
    10240: "1507", 10586: "1511", 14393: "1607", 15063: "1703", 16299: "1709", 17134: "1803",
    17763: "1809", 18362: "1903", 18363: "1909", 19041: "2004", 19042: "20H2", 19043: "21H1",
    19044: "21H2", 19045: "22H2", 22000: "21H2", 22621: "22H2", 22631: "23H2", 26100: "24H2",
    26200: "25H2", 26300: "26H2",
}


def read_wim_xml(node: Node) -> str | None:
    """Return the XML index of a WIM/ESD file inside the image tree."""
    if node is None or node.is_dir or node.size < 208:
        return None
    hdr = node.read_range(0, 208)
    if hdr[:8] != b"MSWIM\0\0\0":
        return None
    size_flags, offset, _ = struct.unpack_from("<QQQ", hdr, 0x48)
    size, flags = size_flags & 0x00FFFFFFFFFFFFFF, size_flags >> 56
    if size == 0 or size > 64 << 20 or flags & 0x04 or offset + size > node.size:
        return None
    data = node.read_range(offset, size)
    text = data.decode("utf-16-le", "replace")
    return text.lstrip("﻿")


def parse_wim_xml(text: str) -> list[dict]:
    images = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return images
    for img in root.findall("IMAGE"):
        win = img.find("WINDOWS")

        def val(path, base=img):
            el = base.find(path) if base is not None else None
            return el.text.strip() if el is not None and el.text else None

        info = {
            "index": int(img.get("INDEX", "0") or 0),
            "name": val("NAME"),
            "display_name": val("DISPLAYNAME") or val("NAME"),
            "description": val("DESCRIPTION"),
        }
        if win is not None:
            arch = val("ARCH", win)
            info["arch"] = ARCH_NAMES.get(int(arch)) if arch and arch.isdigit() else None
            info["edition"] = val("EDITIONID", win)
            info["major"] = _int(val("VERSION/MAJOR", win))
            info["minor"] = _int(val("VERSION/MINOR", win))
            info["build"] = _int(val("VERSION/BUILD", win))
            info["spbuild"] = _int(val("VERSION/SPBUILD", win))
            langs = win.find("LANGUAGES")
            if langs is not None:
                info["languages"] = [l.text.strip() for l in langs.findall("LANGUAGE") if l.text]
                info["default_language"] = val("DEFAULT", langs)
        images.append(info)
    return images


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


class WindowsInfo:
    """What kind of Windows install media this is."""

    def __init__(self):
        self.is_windows = False
        self.arch: str | None = None
        self.major = 0          # 10 or 11 (Rufus style: build > 20000 means 11)
        self.build = 0
        self.spbuild = 0
        self.languages: list[str] = []
        self.editions: list[str] = []
        self.install_image: str | None = None
        self.boot_wim: str | None = None

    @property
    def is_win11(self) -> bool:
        return self.major >= 11

    def describe(self) -> str:
        if not self.is_windows:
            return "not recognised as Windows install media"
        parts = [f"Windows {self.major}" if self.major else "Windows"]
        fu = FEATURE_UPDATES.get(self.build)
        if fu:
            parts[0] += f" {fu}"
        if self.build:
            parts.append(f"build {self.build}" + (f".{self.spbuild}" if self.spbuild else ""))
        if self.arch:
            parts.append({"amd64": "x64"}.get(self.arch, self.arch))
        if self.languages:
            parts.append(", ".join(self.languages[:3]) + ("…" if len(self.languages) > 3 else ""))
        return " · ".join(parts)


def detect_windows(root: Node) -> WindowsInfo:
    info = WindowsInfo()
    for name in ("sources/install.wim", "sources/install.esd", "sources/install.swm"):
        if root.find(name) is not None:
            info.install_image = name
            break
    if root.find("sources/boot.wim") is not None:
        info.boot_wim = "sources/boot.wim"
    info.is_windows = bool(info.install_image or info.boot_wim) and root.find("sources") is not None

    images = []
    if info.install_image:
        images = parse_wim_xml(read_wim_xml(root.find(info.install_image)) or "")
    if images:
        first = images[0]
        info.editions = [i.get("display_name") or i.get("name") or f"Index {i['index']}" for i in images]
        _apply_version(info, first)
        info.arch = first.get("arch")
    boot_images = parse_wim_xml(read_wim_xml(root.find("sources/boot.wim")) or "") if info.boot_wim else []
    if boot_images:
        setup = boot_images[1] if len(boot_images) > 1 else boot_images[0]
        if not info.build:
            _apply_version(info, setup)
        info.arch = info.arch or setup.get("arch")
        if not info.languages and setup.get("languages"):
            info.languages = list(setup["languages"])
    if not info.languages:
        info.languages = _lang_ini(root) or (images[0].get("languages", []) if images else [])
    if not info.arch:
        for efi, arch in (("bootx64.efi", "amd64"), ("bootaa64.efi", "arm64"),
                          ("bootia32.efi", "x86"), ("bootarm.efi", "arm")):
            if root.find(f"efi/boot/{efi}") is not None:
                info.arch = arch
                break
    return info


def _apply_version(info: WindowsInfo, img: dict):
    major, minor, build = img.get("major") or 0, img.get("minor") or 0, img.get("build") or 0
    if major == 6 and minor == 4:
        major = 10
    if major == 10 and build > 20000:
        major = 11
    info.major, info.build, info.spbuild = major, build, img.get("spbuild") or 0


def _lang_ini(root: Node) -> list[str]:
    node = root.find("sources/lang.ini")
    if node is None or node.size > 64 * 1024:
        return []
    try:
        text = node.read().decode("utf-16") if node.read_range(0, 2) in (b"\xff\xfe", b"\xfe\xff") \
            else node.read().decode("latin-1")
    except UnicodeDecodeError:
        return []
    langs, section = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.lower()
        elif section == "[available ui languages]" and "=" in line:
            langs.append(line.split("=")[0].strip())
    return langs


# --------------------------------------------------------------------------- #
# File extraction (wimlib or 7-Zip)
# --------------------------------------------------------------------------- #

def extractor() -> tuple[str, str] | None:
    """Return (kind, executable) of an available WIM extraction tool."""
    for kind, exe in (("wimlib", "wimlib-imagex"), ("7z", "7zz"), ("7z", "7z"), ("7z", "7za")):
        path = shutil.which(exe)
        if path:
            return kind, path
    return None


def extract_dirs(wim_path: str, index: int, dirs: list[str], dest: str) -> None:
    """Extract directories (e.g. 'Windows/Boot/EFI_EX') from image `index` into `dest`.

    Missing directories are skipped. Raises RuntimeError when no tool is available
    or the tool fails outright.
    """
    tool = extractor()
    if tool is None:
        raise RuntimeError("needs wimlib (wimlib-imagex) or 7-Zip (7zz/7z) to read boot.wim")
    kind, exe = tool
    os.makedirs(dest, exist_ok=True)
    for d in dirs:
        if kind == "wimlib":
            cmd = [exe, "extract", wim_path, str(index), "/" + d, f"--dest-dir={dest}",
                   "--no-acls", "--nullglob"]
        else:
            cmd = [exe, "x", "-y", f"-o{dest}", wim_path, f"{index}/{d}"]
        env = dict(os.environ, WIMLIB_IMAGEX_IGNORE_CASE="1")  # Windows paths are case-insensitive
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"{os.path.basename(exe)} failed: {r.stdout.strip()[-400:]}")


def find_file(base: str, *names: str) -> str | None:
    """Find a file below `base` whose path ends with the given components (case-insensitive)."""
    want = [n.lower() for n in names]
    for dirpath, _, files in os.walk(base):
        for f in files:
            full = os.path.join(dirpath, f)
            parts = [p.lower() for p in full.split(os.sep)]
            if parts[-len(want):] == want:
                return full
    return None


def list_files(base: str, dirname: str) -> list[str]:
    """All files inside any directory named `dirname` below `base`."""
    out = []
    for dirpath, _, files in os.walk(base):
        if os.path.basename(dirpath).lower() == dirname.lower():
            out.extend(os.path.join(dirpath, f) for f in sorted(files))
    return out
