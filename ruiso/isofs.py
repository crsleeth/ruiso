"""Read ISO images (UDF, Joliet or plain ISO9660) into an in-memory file tree.

Nothing is mounted or extracted: file contents stay in the source image and are
only referenced by their byte ranges, so building a new image is a single
sequential copy.
"""
from __future__ import annotations

import datetime as _dt
import os
import struct
import unicodedata

SECTOR = 2048


class IsoError(Exception):
    pass


# --------------------------------------------------------------------------- #
# File tree
# --------------------------------------------------------------------------- #

class Node:
    """A file or directory in the image tree."""

    def __init__(self, name: str, is_dir: bool):
        self.name = name
        self.is_dir = is_dir
        self.children: list[Node] = []
        self.size = 0
        # Where the file data comes from:
        #   ("image", IsoImage, [(byte_offset or None, length), ...])  None = zeros
        #   ("bytes", b"...")
        #   ("file", "/local/path")
        self.source: tuple | None = None
        self.mtime = None      # raw 12-byte UDF timestamp or datetime
        self.atime = None
        self.attrtime = None
        self.perms: int | None = None
        self.uid: int | None = None
        self.gid: int | None = None
        self.hidden = False

    def __repr__(self):
        kind = "dir" if self.is_dir else f"{self.size} bytes"
        return f"<Node {self.name!r} {kind}>"

    # -- tree helpers (paths use '/', matching is case-insensitive like Windows)
    def child(self, name: str) -> Node | None:
        low = name.lower()
        for c in self.children:
            if c.name.lower() == low:
                return c
        return None

    def find(self, path: str) -> Node | None:
        node = self
        for part in _split(path):
            if not node.is_dir:
                return None
            node = node.child(part)
            if node is None:
                return None
        return node

    def mkdirs(self, path: str) -> Node:
        node = self
        for part in _split(path):
            nxt = node.child(part)
            if nxt is None:
                nxt = Node(part, True)
                nxt.mtime = _now()
                node.children.append(nxt)
            elif not nxt.is_dir:
                raise IsoError(f"'{part}' exists and is not a directory")
            node = nxt
        return node

    def put_file(self, path: str, data: bytes | None = None, local: str | None = None) -> Node:
        """Create or replace a file. Keeps the existing entry's name casing."""
        parts = _split(path)
        parent = self.mkdirs("/".join(parts[:-1]))
        node = parent.child(parts[-1])
        if node is None:
            node = Node(parts[-1], False)
            parent.children.append(node)
        elif node.is_dir:
            raise IsoError(f"'{path}' is a directory")
        if local is not None:
            node.source = ("file", local)
            node.size = os.path.getsize(local)
        else:
            node.source = ("bytes", bytes(data or b""))
            node.size = len(data or b"")
        node.mtime = _now()
        return node

    def remove(self, path: str) -> Node | None:
        parts = _split(path)
        parent = self.find("/".join(parts[:-1]))
        if parent is None:
            return None
        node = parent.child(parts[-1])
        if node is not None:
            parent.children.remove(node)
        return node

    def rename(self, path: str, new_name: str) -> Node | None:
        node = self.find(path)
        if node is not None:
            parts = _split(path)
            parent = self.find("/".join(parts[:-1]))
            old = parent.child(new_name)
            if old is not None and old is not node:
                parent.children.remove(old)
            node.name = new_name
        return node

    def walk(self, prefix: str = ""):
        """Yield (path, node) for every node below this one, depth first."""
        for c in self.children:
            p = f"{prefix}/{c.name}" if prefix else c.name
            yield p, c
            if c.is_dir:
                yield from c.walk(p)

    # -- data access
    def read(self) -> bytes:
        return b"".join(self.iter_data())

    def iter_data(self, chunk: int = 4 << 20):
        src = self.source
        if src is None or self.size == 0:
            return
        kind = src[0]
        if kind == "bytes":
            yield src[1]
        elif kind == "file":
            with open(src[1], "rb") as f:
                while True:
                    b = f.read(chunk)
                    if not b:
                        break
                    yield b
        elif kind == "image":
            img, extents = src[1], src[2]
            for off, length in extents:
                while length > 0:
                    n = min(chunk, length)
                    yield bytes(n) if off is None else img.read(off, n)
                    if off is not None:
                        off += n
                    length -= n
        else:
            raise IsoError(f"unknown source {kind}")

    def read_range(self, offset: int, length: int) -> bytes:
        """Read part of a file (used to peek at headers of big files like install.wim)."""
        src = self.source
        if src is None:
            return b""
        if src[0] == "bytes":
            return src[1][offset:offset + length]
        if src[0] == "file":
            with open(src[1], "rb") as f:
                f.seek(offset)
                return f.read(length)
        out = bytearray()
        pos = 0
        for off, elen in src[2]:
            if length <= 0:
                break
            if offset < pos + elen:
                start = max(0, offset - pos)
                n = min(elen - start, length)
                out += bytes(n) if off is None else src[1].read(off + start, n)
                offset += n
                length -= n
            pos += elen
        return bytes(out)


def _split(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def _now():
    return _dt.datetime.now().astimezone()


# --------------------------------------------------------------------------- #
# El Torito boot entries
# --------------------------------------------------------------------------- #

class BootEntry:
    """One El Torito boot image (BIOS or UEFI)."""

    def __init__(self, platform: int, media: int = 0, load_seg: int = 0, sys_type: int = 0,
                 sector_count: int = 0, bootable: bool = True):
        self.platform = platform          # 0 = x86 BIOS, 0xEF = UEFI
        self.media = media                # 0 = no emulation
        self.load_seg = load_seg
        self.sys_type = sys_type
        self.sector_count = sector_count  # in 512-byte units, as stored in the catalog
        self.bootable = bootable
        self.path: str | None = None      # file inside the tree holding the image
        self.data: bytes | None = None    # raw image when it isn't a visible file
        self.selection_id = b""           # section header ID string

    @property
    def label(self) -> str:
        return {0: "BIOS", 0xEF: "UEFI"}.get(self.platform, f"platform 0x{self.platform:02x}")


class BootCatalog:
    def __init__(self):
        self.entries: list[BootEntry] = []
        self.validation_id = b""


# --------------------------------------------------------------------------- #
# Image reader
# --------------------------------------------------------------------------- #

def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def decode_dstring(b: bytes) -> str:
    """Decode an OSTA CS0 'dstring' (last byte holds the used length)."""
    if not b or b[0] == 0:
        return ""
    used = b[-1]
    if not 0 < used <= len(b) - 1:
        used = len(b) - 1
    return decode_cs0(b[:used]).rstrip("\x00")


def decode_cs0(b: bytes) -> str:
    if not b:
        return ""
    comp, rest = b[0], b[1:]
    if comp in (16, 255):
        return rest[: len(rest) & ~1].decode("utf-16-be", "replace")
    return rest.decode("latin-1")


class IsoImage:
    """An ISO file opened for reading."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.size = self.f.seek(0, os.SEEK_END)   # works for block devices (/dev/sr0) too
        self.fs = None                     # "udf", "joliet" or "iso9660"
        self.root: Node | None = None
        self.boot = BootCatalog()
        self.pvd: bytes | None = None      # ISO9660 primary volume descriptor
        self.joliet: bytes | None = None
        self.boot_catalog_lba: int | None = None
        self.iso_readme: bytes | None = None
        self.udf_ids: dict = {}            # identifiers copied into the new image
        self._part_starts: list[int] = []
        self.load()

    def close(self):
        self.f.close()

    # -- raw access
    def read(self, offset: int, n: int) -> bytes:
        self.f.seek(offset)
        b = self.f.read(n)
        if len(b) != n:
            raise IsoError(f"unexpected end of image at offset {offset}")
        return b

    def sector(self, lba: int, count: int = 1) -> bytes:
        return self.read(lba * SECTOR, count * SECTOR)

    # -- top level
    def load(self):
        if self.size < 18 * SECTOR:
            raise IsoError("file is too small to be an ISO image")
        self._read_iso9660_descriptors()
        try:
            root = self._read_udf()
        except IsoError as e:
            # Broken UDF: only fall back if the ISO9660 side has a real file tree.
            if self.joliet is None:
                raise IsoError(f"can't read the UDF file system: {e}") from None
            root = None
        if root is not None:
            self.fs = "udf"
        elif self.joliet is not None:
            root = self._read_iso_tree(self.joliet, joliet=True)
            self.fs = "joliet"
        elif self.pvd is not None:
            root = self._read_iso_tree(self.pvd, joliet=False)
            self.fs = "iso9660"
        else:
            raise IsoError("not an ISO image (no ISO9660 or UDF file system found)")
        self.root = root
        self._read_boot_catalog()

    # -- ISO9660 ------------------------------------------------------------
    def _read_iso9660_descriptors(self):
        lba = 16
        while lba < 64 and (lba + 1) * SECTOR <= self.size:
            s = self.sector(lba)
            ident = s[1:6]
            if ident != b"CD001":
                if ident in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
                    lba += 1
                    continue
                break
            t = s[0]
            if t == 0 and s[7:30] == b"EL TORITO SPECIFICATION":
                self.boot_catalog_lba = _u32(s, 71)
            elif t == 1 and self.pvd is None:
                self.pvd = s
            elif t == 2 and s[88:91] in (b"%/@", b"%/C", b"%/E"):
                self.joliet = s
            elif t == 255:
                break
            lba += 1
        if self.pvd is not None:
            # Microsoft images carry a README.TXT on the ISO9660 side; keep it.
            try:
                tree = self._read_iso_tree(self.pvd, joliet=False, max_depth=0)
                readme = tree.child("README.TXT")
                if readme is not None and 0 < readme.size < 64 * 1024:
                    self.iso_readme = readme.read()
            except IsoError:
                pass

    def _read_iso_tree(self, vd: bytes, joliet: bool, max_depth: int = 64) -> Node:
        rec = vd[156:190]
        root = Node("", True)
        self._iso_fill(root, _u32(rec, 2), _u32(rec, 10), joliet, 0, max_depth, set())
        return root

    def _iso_fill(self, parent: Node, extent: int, size: int, joliet: bool, depth: int,
                  max_depth: int, seen: set):
        if extent in seen or size > 64 << 20:
            return
        seen.add(extent)
        data = self.read(extent * SECTOR, size)
        pos = 0
        multi: dict[str, list] = {}
        while pos < len(data):
            n = data[pos]
            if n == 0:
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            r = data[pos:pos + n]
            pos += n
            if len(r) < 34:
                break
            nl = r[32]
            raw = r[33:33 + nl]
            if raw in (b"\x00", b"\x01"):
                continue
            name = raw.decode("utf-16-be", "replace") if joliet else raw.decode("latin-1")
            name = name.split(";")[0]
            if not joliet and name.endswith("."):
                name = name[:-1]
            flags = r[25]
            ext, sz = _u32(r, 2), _u32(r, 10)
            if flags & 0x02:
                node = Node(name, True)
                node.mtime = _iso_date7(r[18:25])
                node.hidden = bool(flags & 0x01)
                parent.children.append(node)
                if depth < max_depth:
                    self._iso_fill(node, ext, sz, joliet, depth + 1, max_depth, seen)
                continue
            if flags & 0x80:  # multi-extent file, more parts follow
                multi.setdefault(name, []).append((ext * SECTOR, sz))
                continue
            extents = multi.pop(name, []) + [(ext * SECTOR, sz)]
            node = Node(name, False)
            node.size = sum(e[1] for e in extents)
            node.source = ("image", self, extents)
            node.mtime = _iso_date7(r[18:25])
            node.hidden = bool(flags & 0x01)
            parent.children.append(node)

    # -- El Torito ----------------------------------------------------------
    def _read_boot_catalog(self):
        if self.boot_catalog_lba is None:
            return
        cat = self.sector(self.boot_catalog_lba)
        if cat[0] != 1 or cat[30:32] != b"\x55\xaa":
            return
        self.boot.validation_id = cat[4:28]
        entries = []

        def parse(e: bytes, platform: int, sel_id: bytes = b""):
            if e[0] not in (0x88, 0x00):
                return None
            be = BootEntry(platform, media=e[1] & 0x0F, load_seg=_u16(e, 2), sys_type=e[4],
                           sector_count=_u16(e, 6), bootable=(e[0] == 0x88))
            be.selection_id = sel_id
            be._rba = _u32(e, 8)
            return be

        first = parse(cat[32:64], cat[1])
        if first is not None:
            entries.append(first)
        pos = 64
        while pos + 32 <= SECTOR:
            h = cat[pos]
            if h not in (0x90, 0x91):
                break
            platform, count, sel_id = cat[pos + 1], _u16(cat, pos + 2), cat[pos + 4:pos + 32]
            pos += 32
            for _ in range(count):
                if pos + 32 > SECTOR:
                    break
                e = parse(cat[pos:pos + 32], platform, sel_id)
                pos += 32
                if e is not None:
                    entries.append(e)
                while pos + 32 <= SECTOR and cat[pos] == 0x44:  # extension records
                    pos += 32
            if h == 0x91:
                break

        # Map each boot image back to the file that holds it.
        by_offset = {}
        for path, node in self.root.walk():
            if not node.is_dir and node.source and node.source[0] == "image" and node.source[2]:
                off = node.source[2][0][0]
                if off is not None:
                    by_offset.setdefault(off, (path, node))
        for e in entries:
            hit = by_offset.get(e._rba * SECTOR)
            if hit is not None:
                e.path = hit[0]
            else:
                e.data = self._raw_boot_image(e)
            self.boot.entries.append(e)

    def _raw_boot_image(self, e: BootEntry) -> bytes:
        start = e._rba * SECTOR
        size = e.sector_count * 512
        if e.platform == 0xEF and e.sector_count <= 1:
            # Size isn't stored: read it from the FAT boot sector of the image.
            bpb = self.read(start, 512)
            bps = _u16(bpb, 11) or 512
            total = _u16(bpb, 19) or _u32(bpb, 32)
            size = bps * total
        size = max(512, min(size, 64 << 20, self.size - start))
        return self.read(start, size)

    # -- UDF ----------------------------------------------------------------
    def _read_udf(self) -> Node | None:
        last = self.size // SECTOR - 1
        avdp = None
        for lba in (256, last, last - 256):
            if lba < 256 or (lba + 1) * SECTOR > self.size:
                continue
            s = self.sector(lba)
            if _u16(s, 0) == 2 and _tag_checksum_ok(s):
                avdp = s
                break
        if avdp is None:
            return None
        main = struct.unpack_from("<II", avdp, 16)
        reserve = struct.unpack_from("<II", avdp, 24)
        err = None
        for length, loc in (main, reserve):
            try:
                return self._udf_from_vds(loc, length)
            except (IsoError, struct.error, KeyError) as e:
                err = e
        raise IsoError(f"unreadable UDF volume: {err}")

    def _udf_from_vds(self, loc: int, length: int) -> Node:
        parts = {}
        lvd = pvd = iuvd = None
        lba, end, steps = loc, loc + max(1, length // SECTOR), 0
        while lba < end and steps < 512:
            steps += 1
            s = self.sector(lba)
            tid = _u16(s, 0)
            if tid == 1:
                pvd = s
            elif tid == 3:  # volume descriptor pointer
                nlen, nloc = struct.unpack_from("<II", s, 20)
                lba, end = nloc, nloc + max(1, nlen // SECTOR)
                continue
            elif tid == 4:
                iuvd = s
            elif tid == 5:
                parts[_u16(s, 22)] = (_u32(s, 188), _u32(s, 192))
            elif tid == 6:
                lvd = s
            elif tid in (0, 8):
                break
            lba += 1
        if lvd is None or not parts:
            raise IsoError("UDF volume descriptors missing")
        if _u32(lvd, 212) != SECTOR:
            raise IsoError(f"unsupported UDF block size {_u32(lvd, 212)}")
        nmaps, off = _u32(lvd, 268), 440
        self._part_starts = []
        for _ in range(nmaps):
            mtype, mlen = lvd[off], lvd[off + 1]
            if mtype != 1:
                raise IsoError("UDF metadata/virtual partitions are not supported")
            self._part_starts.append(parts[_u16(lvd, off + 4)][0])
            off += mlen
        self.udf_ids = {
            "volume_id": decode_dstring(pvd[24:56]) if pvd else "",
            "volume_set_id": decode_dstring(pvd[72:200]) if pvd else "",
            "logical_volume_id": decode_dstring(lvd[84:212]),
            "impl_id": bytes(lvd[272:304]),
        }
        _, fsd_lbn, fsd_ref = struct.unpack_from("<IIH", lvd, 248)
        fsd = self._lb(fsd_ref, fsd_lbn)
        if _u16(fsd, 0) != 256:
            raise IsoError("UDF file set descriptor not found")
        self.udf_ids["file_set_id"] = decode_dstring(fsd[304:336])
        _, root_lbn, root_ref = struct.unpack_from("<IIH", fsd, 400)
        root = Node("", True)
        info = self._udf_entry(root_ref, root_lbn)
        _apply_entry(root, info)
        self._udf_fill_dir(root, info, 0, {(root_ref, root_lbn)})
        return root

    def _abs(self, ref: int, lbn: int) -> int:
        return (self._part_starts[ref] + lbn) * SECTOR

    def _lb(self, ref: int, lbn: int) -> bytes:
        return self.read(self._abs(ref, lbn), SECTOR)

    def _udf_entry(self, ref: int, lbn: int) -> dict:
        b = self._lb(ref, lbn)
        tid = _u16(b, 0)
        if tid == 261:    # File Entry
            l_ea, l_ad = struct.unpack_from("<II", b, 168)
            ad_off = 176 + l_ea
            times = (b[72:84], b[84:96], b[96:108])
        elif tid == 266:  # Extended File Entry
            l_ea, l_ad = struct.unpack_from("<II", b, 208)
            ad_off = 216 + l_ea
            times = (b[80:92], b[92:104], b[116:128])
        else:
            raise IsoError(f"bad UDF file entry at block {lbn}")
        uid, gid, perms = struct.unpack_from("<III", b, 36)
        info_len = _u64(b, 56)
        ad_type = _u16(b, 34) & 7
        ads = b[ad_off:ad_off + l_ad]
        if ad_type == 3:
            extents = ("bytes", bytes(ads[:info_len]))
        else:
            extents = ("image", self, self._udf_extents(ads, ad_type, ref, info_len))
        return {"file_type": b[27], "size": info_len, "source": extents, "uid": uid, "gid": gid,
                "perms": perms, "atime": times[0], "mtime": times[1], "attrtime": times[2]}

    def _udf_extents(self, ads: bytes, ad_type: int, ref: int, info_len: int) -> list:
        step = {0: 8, 1: 16, 2: 20}.get(ad_type)
        if step is None:
            raise IsoError(f"unknown UDF allocation type {ad_type}")
        out, remaining, pos, hops = [], info_len, 0, 0
        while remaining > 0 and pos + step <= len(ads):
            raw = _u32(ads, pos)
            etype, elen = raw >> 30, raw & 0x3FFFFFFF
            if elen == 0:
                break
            if ad_type == 0:
                loc, p = _u32(ads, pos + 4), ref
            elif ad_type == 1:
                loc, p = _u32(ads, pos + 4), _u16(ads, pos + 8)
            else:
                loc, p = _u32(ads, pos + 12), _u16(ads, pos + 16)
            pos += step
            if etype == 3:  # continues in an Allocation Extent Descriptor
                hops += 1
                if hops > 4096:
                    raise IsoError("allocation descriptor loop")
                aed = self._lb(p, loc)
                ads, pos = aed[24:24 + _u32(aed, 20)], 0
                continue
            n = min(elen, remaining)
            out.append((self._abs(p, loc) if etype == 0 else None, n))
            remaining -= n
        if remaining > 0:
            raise IsoError("file data shorter than its recorded size")
        return out

    def _udf_fill_dir(self, node: Node, info: dict, depth: int, seen: set):
        if depth > 64:
            raise IsoError("directory tree too deep")
        tmp = Node("", False)
        tmp.size, tmp.source = info["size"], info["source"]
        data = tmp.read()
        pos = 0
        while pos + 38 <= len(data):
            if _u16(data, pos) != 257:
                break
            chars, lfi = data[pos + 18], data[pos + 19]
            _, icb_lbn, icb_ref = struct.unpack_from("<IIH", data, pos + 20)
            liu = _u16(data, pos + 36)
            raw_name = data[pos + 38 + liu: pos + 38 + liu + lfi]
            pos += (38 + liu + lfi + 3) & ~3
            if chars & 0x0C:  # parent or deleted
                continue
            child_info = self._udf_entry(icb_ref, icb_lbn)
            is_dir = bool(chars & 0x02) or child_info["file_type"] == 4
            child = Node(decode_cs0(raw_name), is_dir)
            child.hidden = bool(chars & 0x01)
            _apply_entry(child, child_info)
            node.children.append(child)
            if is_dir:
                key = (icb_ref, icb_lbn)
                if key in seen:
                    continue
                seen.add(key)
                self._udf_fill_dir(child, child_info, depth + 1, seen)


def _apply_entry(node: Node, info: dict):
    node.uid, node.gid, node.perms = info["uid"], info["gid"], info["perms"]
    node.atime, node.mtime, node.attrtime = info["atime"], info["mtime"], info["attrtime"]
    if not node.is_dir:
        node.size = info["size"]
        node.source = info["source"]


def _tag_checksum_ok(b: bytes) -> bool:
    return (sum(b[0:4]) + sum(b[5:16])) & 0xFF == b[4]


def _iso_date7(b: bytes):
    try:
        tz = _dt.timezone(_dt.timedelta(minutes=15 * struct.unpack("b", b[6:7])[0]))
        return _dt.datetime(1900 + b[0], b[1], b[2], b[3], b[4], b[5], tzinfo=tz)
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Folder input (an already extracted ISO)
# --------------------------------------------------------------------------- #

def tree_from_directory(path: str) -> Node:
    root = Node("", True)
    root.mtime = _now()

    def fill(node: Node, d: str):
        with os.scandir(d) as it:
            for e in sorted(it, key=lambda e: e.name.lower()):
                if e.name in (".DS_Store",) or e.name.startswith("._"):
                    continue
                name = unicodedata.normalize("NFC", e.name)
                st = e.stat()
                mtime = _dt.datetime.fromtimestamp(st.st_mtime).astimezone()
                if e.is_dir():
                    c = Node(name, True)
                    c.mtime = mtime
                    node.children.append(c)
                    fill(c, e.path)
                elif e.is_file():
                    c = Node(name, False)
                    c.size = st.st_size
                    c.source = ("file", e.path)
                    c.mtime = mtime
                    node.children.append(c)

    fill(root, path)
    return root


def default_boot_entries(root: Node) -> list[BootEntry]:
    """Boot entries for a Windows tree that has no boot catalog of its own."""
    entries = []
    bios = root.find("boot/etfsboot.com")
    if bios is not None:
        e = BootEntry(0, sector_count=8)
        e.path = "boot/etfsboot.com"
        entries.append(e)
    for name in ("efi/microsoft/boot/efisys.bin", "efi/microsoft/boot/efisys_noprompt.bin"):
        if root.find(name) is not None:
            e = BootEntry(0xEF, sector_count=1)
            e.path = name
            entries.append(e)
            break
    return entries
