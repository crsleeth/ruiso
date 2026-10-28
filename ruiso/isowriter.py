"""Write a bootable ISO laid out the way Microsoft's own Windows ISOs are.

Like `oscdimg -u2 -udfver102`, the image has:
  * an ISO9660 side that only holds README.TXT plus the El Torito boot catalog
    (BIOS + UEFI entries), and
  * a UDF 1.02 file system with the real file tree (handles files > 4 GB).
"""
from __future__ import annotations

import binascii
import datetime as _dt
import os
import random
import struct
import time

from .isofs import SECTOR, BootEntry, IsoError, Node

# Fixed sector layout in front of the UDF partition
ISO_PVD, ISO_BOOT, ISO_TERM = 16, 17, 18
VRS_BEA, VRS_NSR, VRS_TEA = 19, 20, 21
MAIN_VDS, RESERVE_VDS, VDS_LEN = 32, 48, 16
LVID = 64
ISO_LPT, ISO_MPT, ISO_ROOT, ISO_CAT, ISO_README = 66, 67, 68, 69, 70
AVDP = 256
PART_START = 257

UDF_REV = 0x0102
MAX_EXTENT = 0x3FF00000          # extent size Microsoft uses for big files (< 1 GB, whole blocks)
DEFAULT_PERMS = 0x35AD           # what Microsoft uses: read/execute/chattr for everyone
NO_ID = 0xFFFFFFFF

README_DEFAULT = (b'This disc contains a "UDF" file system and requires an operating system\r\n'
                  b'that supports the ISO-13346 "UDF" file system specification.\r\n')

CHARSPEC = bytes([0]) + b"OSTA Compressed Unicode".ljust(63, b"\0")


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #

def _blocks(n: int) -> int:
    return (n + SECTOR - 1) // SECTOR


def _pad4(n: int) -> int:
    return (n + 3) & ~3


def _pad_sector(b) -> bytes:
    rem = len(b) % SECTOR
    return bytes(b) + bytes(SECTOR - rem) if rem else bytes(b)


def cs0(name: str) -> bytes:
    try:
        return b"\x08" + name.encode("latin-1")
    except UnicodeEncodeError:
        return b"\x10" + name.encode("utf-16-be")


def dstring(s: str, size: int) -> bytes:
    out = bytearray(size)
    if s:
        b = cs0(s)
        if len(b) > size - 1:
            b = b[: size - 1] if b[0] == 8 else b[: 1 + ((size - 2) & ~1)]
        out[: len(b)] = b
        out[size - 1] = len(b)
    return bytes(out)


def regid(ident: bytes, suffix: bytes = b"", flags: int = 0) -> bytes:
    return bytes([flags]) + ident.ljust(23, b"\0")[:23] + suffix.ljust(8, b"\0")[:8]


DOMAIN_ID = regid(b"*OSTA UDF Compliant", struct.pack("<HB", UDF_REV, 0))
IMPL_ID = regid(b"*ruiso")


def udf_regid(ident: bytes) -> bytes:
    return regid(ident, struct.pack("<HBB", UDF_REV, 0, 0))


def udf_ts(value, fallback: _dt.datetime) -> bytes:
    if isinstance(value, (bytes, bytearray)) and len(value) == 12 and any(value):
        return bytes(value)
    dt = value if isinstance(value, _dt.datetime) else fallback
    if dt.tzinfo is None:
        dt = dt.astimezone()
    off = int(dt.utcoffset().total_seconds() // 60)
    us = dt.microsecond
    return struct.pack("<HhBBBBBBBB", (1 << 12) | (off & 0x0FFF), dt.year, dt.month, dt.day,
                       dt.hour, dt.minute, dt.second, us // 10000, (us // 100) % 100, us % 100)


def finish_tag(buf: bytearray, tag_id: int, location: int, crc_len: int | None = None) -> bytearray:
    if crc_len is None:
        crc_len = len(buf) - 16
    crc = binascii.crc_hqx(bytes(buf[16:16 + crc_len]), 0)   # CRC-ITU-T, as UDF requires
    struct.pack_into("<HHBBHHHI", buf, 0, tag_id, 2, 0, 0, 0, crc, crc_len, location)
    buf[4] = (sum(buf[0:4]) + sum(buf[5:16])) & 0xFF
    return buf


def long_ad(length: int, lbn: int, unique_id: int = 0) -> bytes:
    return struct.pack("<IIHHI", length, lbn, 0, 0, unique_id & 0xFFFFFFFF)


def short_ads(lbn: int, size: int) -> bytes:
    out = bytearray()
    while size > 0:
        n = min(size, MAX_EXTENT)
        out += struct.pack("<II", n, lbn)
        lbn += n // SECTOR
        size -= n
    return bytes(out)


def _both16(v: int) -> bytes:
    return struct.pack("<H", v) + struct.pack(">H", v)


def _both32(v: int) -> bytes:
    return struct.pack("<I", v) + struct.pack(">I", v)


def _iso_date7(dt: _dt.datetime) -> bytes:
    off = int(dt.utcoffset().total_seconds() // 900)
    return struct.pack("<BBBBBBb", max(0, dt.year - 1900), dt.month, dt.day, dt.hour,
                       dt.minute, dt.second, off)


def _iso_date17(dt: _dt.datetime) -> bytes:
    off = int(dt.utcoffset().total_seconds() // 900)
    return dt.strftime("%Y%m%d%H%M%S").encode() + b"00" + struct.pack("b", off)


def _dir_record(extent: int, size: int, flags: int, name: bytes, when: _dt.datetime) -> bytes:
    n = 33 + len(name)
    n += n & 1
    r = bytearray(n)
    r[0] = n
    r[2:10] = _both32(extent)
    r[10:18] = _both32(size)
    r[18:25] = _iso_date7(when)
    r[25] = flags
    r[28:32] = _both16(1)
    r[32] = len(name)
    r[33:33 + len(name)] = name
    return bytes(r)


# --------------------------------------------------------------------------- #
# Layout planning
# --------------------------------------------------------------------------- #

class _Item:
    __slots__ = ("node", "parent", "kids", "fe", "uid", "dlbn", "dsize")

    def __init__(self, node: Node, parent: _Item | None):
        self.node, self.parent, self.kids = node, parent, []
        self.fe = self.uid = self.dlbn = self.dsize = 0


class Plan:
    """Where every structure goes. Computed before anything is written."""

    def __init__(self, root: Node, boot_entries: list[BootEntry]):
        self.items: list[_Item] = []
        self._collect(root, None)
        self.by_node = {id(it.node): it for it in self.items}

        lbn = 2                                    # 0 = File Set Descriptor, 1 = terminator
        # Boot images that aren't visible files go first, near the start of the disc
        # (Microsoft puts them there too; old BIOSes may not reach far into the disc).
        self.boot_entries = boot_entries
        self.raw_boot_lbn = {}
        for i, e in enumerate(boot_entries):
            if e.data is not None:
                self.raw_boot_lbn[i] = lbn
                lbn += _blocks(len(e.data))
        self.fe_start = lbn
        for it in self.items:
            it.fe = lbn
            lbn += 1
        uid = 16                                   # 0 = root, 1-15 reserved by UDF
        for it in self.items[1:]:
            it.uid = uid
            uid += 1
        self.next_uid = uid
        for it in self.items:
            if it.node.is_dir:
                it.dsize = 40 + sum(_pad4(38 + len(cs0(k.node.name))) for k in it.kids)
                it.dlbn = lbn
                lbn += _blocks(it.dsize)
        self.data_start = lbn
        for it in self.items:
            if not it.node.is_dir and it.node.size > 0:
                it.dlbn = lbn
                lbn += _blocks(it.node.size)
        self.part_len = lbn
        self.total_sectors = PART_START + self.part_len + 1   # + closing anchor
        self.data_bytes = sum(it.node.size for it in self.items if not it.node.is_dir)

    def _collect(self, node: Node, parent: _Item | None):
        it = _Item(node, parent)
        self.items.append(it)
        if parent is not None:
            parent.kids.append(it)
        if node.is_dir:
            node.children.sort(key=lambda c: c.name.upper())
            seen = set()
            for c in node.children:
                key = c.name.upper()
                if key in seen:
                    raise IsoError(f"duplicate name (Windows is case-insensitive): {c.name}")
                seen.add(key)
                if len(cs0(c.name)) > 255:
                    raise IsoError(f"file name too long: {c.name}")
                self._collect(c, it)

    def boot_lba(self, index: int, root: Node) -> int:
        e = self.boot_entries[index]
        if e.data is not None:
            return PART_START + self.raw_boot_lbn[index]
        node = root.find(e.path)
        if node is None or node.is_dir or node.size == 0:
            raise IsoError(f"boot image '{e.path}' is missing from the image")
        return PART_START + self.by_node[id(node)].dlbn


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

def write_image(root: Node, out_path: str, boot_entries: list[BootEntry], *,
                udf_ids: dict | None = None, iso_pvd: bytes | None = None,
                readme: bytes | None = None, validation_id: bytes = b"",
                progress=None) -> Plan:
    """Write `root` to `out_path`. `progress(done_bytes, total_bytes)` is called while copying."""
    now = _dt.datetime.now().astimezone()
    plan = Plan(root, boot_entries)
    ids = dict(udf_ids or {})
    label = ids.get("logical_volume_id") or ids.get("volume_id") or "RUISO"
    ids.setdefault("volume_id", label)
    ids.setdefault("logical_volume_id", label)
    ids.setdefault("file_set_id", label)
    if not ids.get("volume_set_id"):
        ids["volume_set_id"] = f"{int(time.time()):08X}{random.getrandbits(32):08X}{label}"

    head = bytearray(PART_START * SECTOR)

    def put(lba: int, data):
        head[lba * SECTOR: lba * SECTOR + len(data)] = data

    # ---- ISO9660 side: README.TXT + El Torito ----
    readme = readme or README_DEFAULT
    put(ISO_PVD, _iso_pvd(iso_pvd, label, plan.total_sectors, now))
    if boot_entries:
        br = bytearray(SECTOR)
        br[0:7] = b"\x00CD001\x01"
        br[7:39] = b"EL TORITO SPECIFICATION".ljust(32, b"\0")
        struct.pack_into("<I", br, 71, ISO_CAT)
        put(ISO_BOOT, br)
        put(ISO_TERM, b"\xffCD001\x01")
        put(ISO_CAT, _boot_catalog(plan, root, validation_id))
    else:
        # Keep the descriptor sequence contiguous so UDF readers still find BEA01.
        put(ISO_BOOT, b"\xffCD001\x01")
        put(ISO_TERM, b"\xffCD001\x01")
    put(VRS_BEA, b"\x00BEA01\x01")
    put(VRS_NSR, b"\x00NSR02\x01")
    put(VRS_TEA, b"\x00TEA01\x01")
    put(ISO_LPT, struct.pack("<BBIH", 1, 0, ISO_ROOT, 1) + b"\0\0")
    put(ISO_MPT, struct.pack(">BBIH", 1, 0, ISO_ROOT, 1) + b"\0\0")
    put(ISO_ROOT, _dir_record(ISO_ROOT, SECTOR, 2, b"\0", now)
        + _dir_record(ISO_ROOT, SECTOR, 2, b"\1", now)
        + _dir_record(ISO_README, len(readme), 0, b"README.TXT;1", now))
    put(ISO_README, readme[:SECTOR * (AVDP - ISO_README - 1)])

    # ---- UDF volume structures ----
    for base in (MAIN_VDS, RESERVE_VDS):
        for i, d in enumerate(_vds(base, ids, plan.part_len, now)):
            put(base + i, d)
    put(LVID, _lvid(plan, now))
    put(LVID + 1, finish_tag(bytearray(512), 8, LVID + 1))
    put(AVDP, _avdp(AVDP))

    tmp = out_path + ".part"
    try:
        with open(tmp, "wb") as out:
            out.write(head)
            # File Set Descriptor + terminator
            out.write(_pad_sector(_fsd(plan, ids, now)))
            out.write(_pad_sector(finish_tag(bytearray(512), 8, 1)))
            for i, lbn in plan.raw_boot_lbn.items():
                _expect(out, lbn)
                out.write(_pad_sector(boot_entries[i].data))
            _expect(out, plan.fe_start)
            for it in plan.items:
                out.write(_pad_sector(_file_entry(it, now)))
            for it in plan.items:
                if it.node.is_dir:
                    _expect(out, it.dlbn)
                    out.write(_pad_sector(_dir_data(it)))
            done = 0
            for it in plan.items:
                n = it.node
                if n.is_dir or n.size == 0:
                    continue
                _expect(out, it.dlbn)
                written = 0
                for chunk in n.iter_data():
                    out.write(chunk)
                    written += len(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, plan.data_bytes)
                if written != n.size:
                    raise IsoError(f"'{n.name}': expected {n.size} bytes, read {written}")
                if written % SECTOR:
                    out.write(bytes(SECTOR - written % SECTOR))
            _expect(out, plan.part_len)
            out.write(_pad_sector(_avdp(PART_START + plan.part_len)))
            if out.tell() != plan.total_sectors * SECTOR:
                raise IsoError("internal error: image size mismatch")
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return plan


def _expect(out, lbn: int):
    if out.tell() != (PART_START + lbn) * SECTOR:
        raise IsoError(f"internal error: layout mismatch at block {lbn}")


def _iso_pvd(template: bytes | None, label: str, total: int, now: _dt.datetime) -> bytes:
    if template is not None and len(template) == SECTOR and template[0] == 1:
        pvd = bytearray(template)
    else:
        pvd = bytearray(SECTOR)
        pvd[0:7] = b"\x01CD001\x01"
        pvd[8:40] = b" " * 32
        vol = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in label.upper())[:32]
        pvd[40:72] = vol.encode("ascii", "replace").ljust(32, b" ")
        pvd[190:702] = b" " * 512
        pvd[574:579] = b"RUISO"
        pvd[702:813] = b" " * 111
        pvd[813:830] = _iso_date17(now)
        pvd[830:847] = _iso_date17(now)
        pvd[847:864] = b"0" * 16 + b"\0"
        pvd[864:881] = b"0" * 16 + b"\0"
        pvd[881] = 1
    pvd[80:88] = _both32(total)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(SECTOR)
    pvd[132:140] = _both32(10)
    pvd[140:156] = struct.pack("<II", ISO_LPT, 0) + struct.pack(">II", ISO_MPT, 0)
    pvd[156:190] = _dir_record(ISO_ROOT, SECTOR, 2, b"\0", now)
    return bytes(pvd)


def _boot_catalog(plan: Plan, root: Node, validation_id: bytes) -> bytes:
    entries = plan.boot_entries
    cat = bytearray(SECTOR)
    cat[0] = 1
    cat[1] = entries[0].platform
    cat[4:28] = (validation_id or b"")[:24].ljust(24, b"\0")
    cat[30:32] = b"\x55\xaa"
    s = sum(struct.unpack("<16H", bytes(cat[0:32]))) & 0xFFFF
    struct.pack_into("<H", cat, 28, (-s) & 0xFFFF)

    def entry(i: int, off: int):
        e = entries[i]
        struct.pack_into("<BBHBBHI", cat, off, 0x88 if e.bootable else 0, e.media, e.load_seg,
                         e.sys_type, 0, e.sector_count, plan.boot_lba(i, root))

    entry(0, 32)
    groups: list[tuple[int, list[int]]] = []
    for i in range(1, len(entries)):
        if groups and groups[-1][0] == entries[i].platform:
            groups[-1][1].append(i)
        else:
            groups.append((entries[i].platform, [i]))
    off = 64
    for gi, (platform, idx) in enumerate(groups):
        struct.pack_into("<BBH", cat, off, 0x91 if gi == len(groups) - 1 else 0x90, platform, len(idx))
        cat[off + 4:off + 32] = (entries[idx[0]].selection_id or b"")[:28].ljust(28, b"\0")
        off += 32
        for i in idx:
            entry(i, off)
            off += 32
    return bytes(cat)


def _vds(base: int, ids: dict, part_len: int, now) -> list[bytes]:
    pvd = bytearray(512)
    struct.pack_into("<II", pvd, 16, 0, 0)
    pvd[24:56] = dstring(ids["volume_id"], 32)
    struct.pack_into("<HHHHII", pvd, 56, 1, 1, 2, 2, 1, 1)
    pvd[72:200] = dstring(ids["volume_set_id"], 128)
    pvd[200:264] = CHARSPEC
    pvd[264:328] = CHARSPEC
    pvd[344:376] = IMPL_ID
    pvd[376:388] = udf_ts(None, now)
    pvd[388:420] = IMPL_ID
    finish_tag(pvd, 1, base)

    iuvd = bytearray(512)
    struct.pack_into("<I", iuvd, 16, 1)
    iuvd[20:52] = udf_regid(b"*UDF LV Info")
    iuvd[52:116] = CHARSPEC
    iuvd[116:244] = dstring(ids["logical_volume_id"], 128)
    iuvd[352:384] = IMPL_ID
    finish_tag(iuvd, 4, base + 1)

    pd = bytearray(512)
    struct.pack_into("<IHH", pd, 16, 2, 1, 0)
    pd[24:56] = regid(b"+NSR02")
    struct.pack_into("<III", pd, 184, 1, PART_START, part_len)     # read-only
    pd[196:228] = IMPL_ID
    finish_tag(pd, 5, base + 2)

    lvd = bytearray(446)
    struct.pack_into("<I", lvd, 16, 3)
    lvd[20:84] = CHARSPEC
    lvd[84:212] = dstring(ids["logical_volume_id"], 128)
    struct.pack_into("<I", lvd, 212, SECTOR)
    lvd[216:248] = DOMAIN_ID
    lvd[248:264] = long_ad(SECTOR, 0)                              # File Set Descriptor
    struct.pack_into("<II", lvd, 264, 6, 1)
    lvd[272:304] = IMPL_ID
    struct.pack_into("<II", lvd, 432, 2 * SECTOR, LVID)
    struct.pack_into("<BBHH", lvd, 440, 1, 6, 1, 0)                # type 1 partition map
    finish_tag(lvd, 6, base + 3)

    usd = bytearray(24)
    struct.pack_into("<II", usd, 16, 4, 0)
    finish_tag(usd, 7, base + 4)

    td = finish_tag(bytearray(512), 8, base + 5)
    return [bytes(x) for x in (pvd, iuvd, pd, lvd, usd, td)]


def _lvid(plan: Plan, now) -> bytes:
    nfiles = sum(1 for it in plan.items if not it.node.is_dir)
    ndirs = len(plan.items) - nfiles
    b = bytearray(134)
    b[16:28] = udf_ts(None, now)
    struct.pack_into("<I", b, 28, 1)                         # closed
    struct.pack_into("<Q", b, 40, plan.next_uid)             # next unique ID
    struct.pack_into("<IIII", b, 72, 1, 46, 0, plan.part_len)
    b[88:120] = IMPL_ID
    struct.pack_into("<IIHHH", b, 120, nfiles, ndirs, UDF_REV, UDF_REV, UDF_REV)
    return bytes(finish_tag(b, 9, LVID))


def _avdp(lba: int) -> bytes:
    b = bytearray(512)
    struct.pack_into("<IIII", b, 16, VDS_LEN * SECTOR, MAIN_VDS, VDS_LEN * SECTOR, RESERVE_VDS)
    return bytes(finish_tag(b, 2, lba))


def _fsd(plan: Plan, ids: dict, now) -> bytes:
    b = bytearray(512)
    b[16:28] = udf_ts(None, now)
    struct.pack_into("<HHIIII", b, 28, 3, 3, 1, 1, 0, 0)
    b[48:112] = CHARSPEC
    b[112:240] = dstring(ids["logical_volume_id"], 128)
    b[240:304] = CHARSPEC
    b[304:336] = dstring(ids["file_set_id"], 32)
    b[400:416] = long_ad(SECTOR, plan.items[0].fe, 0)
    b[416:448] = DOMAIN_ID
    return bytes(finish_tag(b, 256, 0))


def _file_entry(it: _Item, now) -> bytes:
    n = it.node
    if n.is_dir:
        ftype, size = 4, it.dsize
        links = 1 + sum(1 for k in it.kids if k.node.is_dir)
    else:
        ftype, size, links = 5, n.size, 1
    ads = short_ads(it.dlbn, size)
    if 176 + len(ads) > SECTOR:
        raise IsoError(f"'{n.name}' is too large")
    fe = bytearray(176 + len(ads))
    struct.pack_into("<IHHHBB", fe, 16, 0, 4, 0, 1, 0, ftype)      # ICB tag, strategy 4
    struct.pack_into("<H", fe, 34, 0)                              # short allocation descriptors
    struct.pack_into("<IIIH", fe, 36,
                     NO_ID if n.uid is None else n.uid,
                     NO_ID if n.gid is None else n.gid,
                     DEFAULT_PERMS if n.perms is None else n.perms, links)
    struct.pack_into("<QQ", fe, 56, size, _blocks(size))
    fe[72:84] = udf_ts(n.atime or n.mtime, now)
    fe[84:96] = udf_ts(n.mtime, now)
    fe[96:108] = udf_ts(n.attrtime or n.mtime, now)
    struct.pack_into("<I", fe, 108, 1)
    fe[128:160] = IMPL_ID
    struct.pack_into("<QII", fe, 160, it.uid, 0, len(ads))
    fe[176:] = ads
    return bytes(finish_tag(fe, 261, it.fe))


def _dir_data(it: _Item) -> bytes:
    buf = bytearray()

    def add(chars: int, target: _Item, name: bytes):
        fid = bytearray(_pad4(38 + len(name)))
        struct.pack_into("<HBB", fid, 16, 1, chars, len(name))
        fid[20:36] = long_ad(SECTOR, target.fe, target.uid)
        fid[38:38 + len(name)] = name
        finish_tag(fid, 257, it.dlbn + len(buf) // SECTOR)
        buf.extend(fid)

    add(0x0A, it.parent or it, b"")                                # parent entry
    for k in it.kids:
        add((0x02 if k.node.is_dir else 0) | (0x01 if k.node.hidden else 0), k, cs0(k.node.name))
    if len(buf) != it.dsize:
        raise IsoError("internal error: directory size mismatch")
    return bytes(buf)
