"""Apply the selected Windows User Experience options to an image tree."""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request

from . import unattend, wim
from .isofs import Node

# Rufus' setup.exe wrapper for Windows 11 24H2+ in-place upgrades (see Rufus res/setup).
# Pinned to a commit and SHA-256 (these match the VirusTotal links in Rufus' readme).
WRAPPER_COMMIT = "1a4175891b78dc46e0023e389288dbd0ccd8df8c"
WRAPPER_URL = "https://raw.githubusercontent.com/pbatard/rufus/{commit}/res/setup/setup_{arch}.exe"
WRAPPER_SHA256 = {
    "x64": "11df838dc69378187e1e1aaf32d34384157642d07096c6e49c1d0e7375634544",
    "arm64": "14bd07f559513890a0f6565df3927392b4fe6b8e6fc3f5e832e9d69c8b7bb7eb",
}
PE_AMD64, PE_ARM64 = 0x8664, 0xAA64


class Change:
    def __init__(self, kind: str, text: str):
        self.kind = kind    # "+" added, "~" changed, "!" warning
        self.text = text


class Customizer:
    """Holds temporary files (extracted bootloaders) until the image is written."""

    def __init__(self, root: Node, info, boot_entries, log=print):
        self.root = root
        self.info = info
        self.boot_entries = boot_entries
        self.log = log
        self.changes: list[Change] = []
        self._tmp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()

    def cleanup(self):
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def tmpdir(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="ruiso-")
        return self._tmp

    def note(self, kind: str, text: str):
        self.changes.append(Change(kind, text))

    # ------------------------------------------------------------------ #
    def apply(self, selected: set[str], xml: str | None, *, setup_wrapper: bool = True):
        if xml is not None:
            self._add_answer_file(selected, xml)
        if "tpm" in selected:
            self._upgrade_bypass(setup_wrapper)
        if "ca2023" in selected:
            self._ca2023()

    def _add_answer_file(self, selected: set[str], xml: str):
        path = unattend.answer_file_path(selected)
        for existing in (unattend.ROOT_PATH, unattend.PANTHER_PATH):
            if self.root.find(existing) is not None:
                if existing == path:
                    self.note("!", f"replaced the image's own {existing}")
                else:
                    self.note("!", f"the image already has {existing}; it may take precedence")
        self.root.put_file(path, xml.encode("utf-8"))
        self.note("+", f"{path} ({len(xml.encode('utf-8')):,} bytes)")

    # -- in-place upgrade support (same as Rufus) ------------------------
    def _upgrade_bypass(self, setup_wrapper: bool):
        # An empty appraiserres.dll lets setup.exe upgrade in place without TPM/SB checks.
        dll = self.root.find("sources/appraiserres.dll")
        if dll is not None and dll.size > 0:
            self.root.rename("sources/appraiserres.dll", "appraiserres.bak")
            self.root.put_file("sources/appraiserres.dll", b"")
            self.note("~", "sources/appraiserres.dll → appraiserres.bak (empty placeholder added)")

        # Windows 11 24H2+ needs registry tweaks before setup runs: Rufus swaps in a
        # small signed setup.exe that applies them and then starts the real setup.
        if not setup_wrapper or self.info.build < 26000:
            return
        setup = self.root.find("setup.exe")
        if setup is None or self.root.find("setup.dll") is not None:
            return
        machine = _pe_machine(setup.read_range(0, 4096))
        arch = {PE_AMD64: "x64", PE_ARM64: "arm64"}.get(machine)
        if arch is None:
            self.note("!", "setup.exe isn't x64/arm64; in-place upgrade wrapper not added")
            return
        try:
            data = fetch_wrapper(arch, self.log)
        except Exception as e:  # network trouble shouldn't stop the ISO being built
            self.note("!", f"couldn't get Rufus' setup.exe wrapper ({e}); in-place upgrades from "
                           "this ISO may still check TPM/CPU (clean installs are unaffected)")
            return
        self.root.rename("setup.exe", "setup.dll")
        self.root.put_file("setup.exe", data)
        self.note("~", "setup.exe → setup.dll (Rufus in-place upgrade wrapper added as setup.exe)")

    # -- Windows UEFI CA 2023 bootloaders -------------------------------
    def _ca2023(self):
        boot_wim = self.root.find("sources/boot.wim")
        if boot_wim is None:
            self.note("!", "no sources/boot.wim; 'Windows CA 2023' option ignored")
            return
        if wim.extractor() is None:
            raise RuntimeError("the 'Windows CA 2023' option (-c) needs wimlib or 7-Zip to read "
                               "boot.wim.\n  macOS:  brew install wimlib\n  Ubuntu: sudo apt install wimtools")
        images = wim.parse_wim_xml(wim.read_wim_xml(boot_wim) or "")
        index = 2 if len(images) >= 2 else 1
        tmp = self.tmpdir()
        wim_path = os.path.join(tmp, "boot.wim")
        self.log("Extracting 'Windows CA 2023' bootloaders from boot.wim…")
        with open(wim_path, "wb") as f:
            for chunk in boot_wim.iter_data():
                f.write(chunk)
        dest = os.path.join(tmp, "x")
        wim.extract_dirs(wim_path, index, ["Windows/Boot/EFI_EX", "Windows/Boot/Fonts_EX",
                                            "Windows/Boot/DVD_EX"], dest)
        os.remove(wim_path)

        bootmgfw = wim.find_file(dest, "EFI_EX", "bootmgfw_EX.efi")
        bootmgr = wim.find_file(dest, "EFI_EX", "bootmgr_EX.efi")
        if not bootmgfw:
            self.note("!", "boot.wim has no 2023-signed bootloaders; 'Windows CA 2023' option ignored")
            return
        replaced = 0
        for f in wim.list_files(dest, "Fonts_EX"):
            name = os.path.basename(f).replace("_EX", "")
            self.root.put_file(f"efi/microsoft/boot/fonts/{name}", local=f)
            replaced += 1
        for arch in ("ia32", "x64", "arm", "aa64"):
            path = f"efi/boot/boot{arch}.efi"
            node = self.root.find(path)
            if node is not None:
                old = node.read()
                self.root.put_file(path, local=bootmgfw)
                replaced += 1
                # Newer media (26H2) also carry an identical copy at the root.
                copy = self.root.find("bootmgfw.efi")
                if copy is not None and copy.size == len(old) and copy.read() == old:
                    self.root.put_file("bootmgfw.efi", local=bootmgfw)
                    replaced += 1
                break
        if bootmgr:
            self.root.put_file("bootmgr.efi", local=bootmgr)
            replaced += 1
        elif self.root.find("bootmgr.efi") is not None:
            self.note("!", "boot.wim has no bootmgr_EX.efi; \\bootmgr.efi keeps its original signature")

        # Optical/ISO UEFI boot starts from the El Torito image (a small FAT disk
        # holding the bootloader), so it needs the 2023-signed version too.
        swapped = 0
        for e in self.boot_entries:
            if e.platform != 0xEF:
                continue
            current = (e.path or "efisys.bin").rsplit("/", 1)[-1]
            want = current.lower().replace(".bin", "_ex.bin")
            src = wim.find_file(dest, "DVD_EX", "EFI", "en-US", want) or \
                wim.find_file(dest, want) or wim.find_file(dest, "efisys_EX.bin")
            if src is None:
                continue
            new_path = "efi/microsoft/boot/" + os.path.basename(src)
            self.root.put_file(new_path, local=src)
            old_count = e.sector_count
            e.path, e.data = new_path, None
            if old_count > 1:
                e.sector_count = min(0xFFFF, (os.path.getsize(src) + 511) // 512)
            swapped += 1
        if swapped == 0 and any(e.platform == 0xEF for e in self.boot_entries):
            self.note("!", "boot.wim has no 2023-signed El Torito image (DVD_EX); UEFI boot from "
                           "this ISO still uses the original 2011-signed loader")
        self.note("~", f"replaced {replaced} EFI files with 'Windows UEFI CA 2023' signed versions"
                       + (" (and the UEFI boot image)" if swapped else ""))
        self.note("!", "this ISO will only boot with Secure Boot on PCs that trust 'Windows UEFI CA 2023'")


def _pe_machine(data: bytes) -> int | None:
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if off + 6 > len(data) or data[off:off + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", data, off + 4)[0]


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ruiso")


def fetch_wrapper(arch: str, log=print) -> bytes:
    """Get Rufus' setup.exe wrapper (cached, verified by SHA-256)."""
    want = WRAPPER_SHA256[arch]
    cache = os.path.join(_cache_dir(), f"setup_{arch}-{want[:12]}.exe")
    try:
        with open(cache, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() == want:
            return data
    except OSError:
        pass
    url = WRAPPER_URL.format(commit=WRAPPER_COMMIT, arch=arch)
    log(f"Downloading Rufus' in-place upgrade wrapper (setup_{arch}.exe)…")
    data = _download(url)
    if hashlib.sha256(data).hexdigest() != want:
        raise RuntimeError("downloaded file failed its SHA-256 check")
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "wb") as f:
            f.write(data)
    except OSError:
        pass
    return data


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except Exception as first:
        # Some Python installs lack CA certificates; fall back to curl/wget.
        for cmd in (["curl", "-fsSL", "--max-time", "60", url], ["wget", "-qO-", "--timeout=60", url]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
        raise RuntimeError(str(first)) from None
