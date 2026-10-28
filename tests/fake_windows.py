"""Build a small fake Windows install tree/ISO for tests (no real Windows files)."""
from __future__ import annotations

import os
import struct


def wim_bytes(images: list[dict], payload: int = 4096) -> bytes:
    """A minimal WIM: valid header + uncompressed XML index (enough for detection)."""
    parts = []
    for i, img in enumerate(images, 1):
        langs = "".join(f"<LANGUAGE>{l}</LANGUAGE>" for l in img.get("langs", ["en-US"]))
        parts.append(
            f'<IMAGE INDEX="{i}"><NAME>{img["name"]}</NAME><DISPLAYNAME>{img["name"]}</DISPLAYNAME>'
            f'<WINDOWS><ARCH>{img.get("arch", 9)}</ARCH><EDITIONID>{img.get("edition", "Professional")}</EDITIONID>'
            f'<LANGUAGES>{langs}<DEFAULT>{img.get("langs", ["en-US"])[0]}</DEFAULT></LANGUAGES>'
            f'<VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>{img["build"]}</BUILD>'
            f'<SPBUILD>{img.get("spbuild", 1)}</SPBUILD></VERSION></WINDOWS></IMAGE>')
    xml = ("﻿<WIM><TOTALBYTES>0</TOTALBYTES>" + "".join(parts) + "</WIM>").encode("utf-16-le")
    hdr = bytearray(208)
    hdr[0:8] = b"MSWIM\0\0\0"
    struct.pack_into("<IIII", hdr, 8, 208, 0x10D00, 0, 32768)
    struct.pack_into("<HHI", hdr, 0x28, 1, 1, len(images))
    off = 208 + payload
    struct.pack_into("<QQQ", hdr, 0x48, len(xml) | (0x02 << 56), off, len(xml))
    return bytes(hdr) + bytes(payload) + xml


def pe_bytes(machine: int = 0x8664) -> bytes:
    b = bytearray(1024)
    b[0:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", b, 0x84, machine)
    return bytes(b)


def make_tree(dest: str, build: int = 26200, arch: int = 9, big_install: int = 0,
              langs=("en-US",)) -> str:
    """Create a fake extracted Windows ISO in `dest`. big_install>0 makes install.wim sparse-large."""
    def put(rel: str, data: bytes):
        p = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    put("boot/etfsboot.com", os.urandom(4096))
    put("boot/bcd", os.urandom(16384))
    put("bootmgr", os.urandom(400000))
    put("bootmgr.efi", os.urandom(300000))
    put("efi/boot/bootx64.efi" if arch == 9 else "efi/boot/bootaa64.efi", os.urandom(250000))
    put("efi/microsoft/boot/efisys.bin", os.urandom(1474560))
    put("efi/microsoft/boot/efisys_noprompt.bin", os.urandom(1474560))
    put("efi/microsoft/boot/fonts/segmono_boot.ttf", os.urandom(40000))
    put("setup.exe", pe_bytes(0x8664 if arch == 9 else 0xAA64))
    put("autorun.inf", b"[AutoRun.Amd64]\r\nopen=setup.exe\r\n")
    put("sources/appraiserres.dll", os.urandom(80000))
    put("sources/lang.ini", ("[Available UI Languages]\r\n" + "".join(f"{l} = 3\r\n" for l in langs)
                             + "\r\n[Fallback Languages]\r\n").encode("latin-1"))
    put("sources/boot.wim", wim_bytes([
        {"name": "Microsoft Windows PE", "build": build, "arch": arch, "langs": list(langs)},
        {"name": "Microsoft Windows Setup", "build": build, "arch": arch, "langs": list(langs)},
    ], payload=200000))
    inst = wim_bytes([
        {"name": "Windows 11 Home", "build": build, "arch": arch, "edition": "Core", "langs": list(langs)},
        {"name": "Windows 11 Pro", "build": build, "arch": arch, "langs": list(langs)},
    ], payload=500000)
    p = put("sources/install.wim", inst)
    if big_install:
        # Move the XML to the end of a large sparse file, like a real 5+ GB install.wim
        with open(p, "r+b") as f:
            xml = inst[208 + 500000:]
            off = big_install - len(xml)
            f.truncate(big_install)
            f.seek(off)
            f.write(xml)
            hdr = bytearray(inst[:208])
            struct.pack_into("<QQQ", hdr, 0x48, len(xml) | (0x02 << 56), off, len(xml))
            f.seek(0)
            f.write(hdr)
    for i in range(30):
        put(f"sources/component_{i:02d}.dll", os.urandom(1000 + i * 37))
    put("support/logging/readme.txt", b"logging\r\n")
    return dest
