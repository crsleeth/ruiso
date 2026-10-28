"""End-to-end tests: build a fake Windows ISO, run ruiso on it, read the result back.

Run with:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fake_windows import make_tree  # noqa: E402
from ruiso import cli, isofs, isowriter, unattend, wim  # noqa: E402
from ruiso.options import OPTIONS  # noqa: E402


def build_iso(tree: str, out: str):
    root = isofs.tree_from_directory(tree)
    isowriter.write_image(root, out, isofs.default_boot_entries(root),
                          udf_ids={"logical_volume_id": "CCCOMA_X64FRE_EN-US_DV9"})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ruiso-test-")
        cls.tree = make_tree(os.path.join(cls.tmp, "tree"))
        cls.iso = os.path.join(cls.tmp, "win.iso")
        build_iso(cls.tree, cls.iso)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ruiso(self, *args) -> int:
        return cli.main(list(args))


class TestImage(Base):
    def test_roundtrip_contents(self):
        img = isofs.IsoImage(self.iso)
        self.assertEqual(img.fs, "udf")
        self.assertEqual(img.udf_ids["logical_volume_id"], "CCCOMA_X64FRE_EN-US_DV9")
        n = 0
        for path, node in img.root.walk():
            if node.is_dir:
                continue
            with open(os.path.join(self.tree, *path.split("/")), "rb") as f:
                self.assertEqual(node.read(), f.read(), path)
            n += 1
        self.assertGreater(n, 40)
        self.assertEqual([(e.platform, e.path) for e in img.boot.entries],
                         [(0, "boot/etfsboot.com"), (0xEF, "efi/microsoft/boot/efisys.bin")])
        self.assertIsNotNone(img.iso_readme)
        img.close()

    def test_detect_windows(self):
        img = isofs.IsoImage(self.iso)
        info = wim.detect_windows(img.root)
        self.assertTrue(info.is_windows)
        self.assertEqual((info.major, info.build, info.arch), (11, 26200, "amd64"))
        self.assertEqual(info.languages, ["en-US"])
        self.assertEqual(info.editions, ["Windows 11 Home", "Windows 11 Pro"])
        img.close()

    def test_hidden_boot_images(self):
        """Microsoft keeps the El Torito images as separate hidden copies; keep them, early on disc."""
        root = isofs.tree_from_directory(self.tree)
        bios, efi = isofs.BootEntry(0, sector_count=8), isofs.BootEntry(0xEF, sector_count=1)
        fat = bytearray(os.urandom(1474560))   # size comes from the FAT boot sector, like efisys.bin
        struct.pack_into("<H", fat, 11, 512)
        struct.pack_into("<H", fat, 19, 2880)
        bios.data, efi.data = os.urandom(4096), bytes(fat)
        out = os.path.join(self.tmp, "hidden.iso")
        isowriter.write_image(root, out, [bios, efi], validation_id=b"Microsoft Corporation")
        img = isofs.IsoImage(out)
        got = img.boot.entries
        self.assertEqual([e.data for e in got], [bios.data, efi.data])
        self.assertEqual([e.sector_count for e in got], [8, 1])
        self.assertTrue(all(e._rba < 1000 for e in got))
        self.assertEqual(img.boot.validation_id.rstrip(b"\0"), b"Microsoft Corporation")
        img.close()

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("hdiutil"), "macOS only")
    def test_macos_mounts_it(self):
        mnt = os.path.join(self.tmp, "mnt")
        os.makedirs(mnt, exist_ok=True)
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mnt, self.iso],
                       check=True, stdout=subprocess.DEVNULL)
        try:
            r = subprocess.run(["diff", "-r", self.tree, mnt], stdout=subprocess.PIPE, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
        finally:
            subprocess.run(["hdiutil", "detach", mnt, "-quiet"])


class TestCli(Base):
    def out(self, name):
        return os.path.join(self.tmp, name)

    def read_tree(self, iso):
        img = isofs.IsoImage(iso)
        return img, {p.lower(): n for p, n in img.root.walk()}

    def test_defaults_tm(self):
        out = self.out("tm.iso")
        self.assertEqual(self.ruiso("-i", self.iso, "-o", out, "-tm", "--no-setup-wrapper"), 0)
        img, files = self.read_tree(out)
        xml_text = files["autounattend.xml"].read().decode()
        xml.dom.minidom.parseString(xml_text)
        self.assertIn("BypassTPMCheck", xml_text)
        self.assertIn("BypassNRO", xml_text)
        self.assertIn('processorArchitecture="amd64"', xml_text)
        self.assertEqual(files["sources/appraiserres.dll"].size, 0)
        self.assertGreater(files["sources/appraiserres.bak"].size, 0)
        self.assertNotIn("setup.dll", files)
        self.assertEqual(len(img.boot.entries), 2)
        img.close()

    def test_panther_placement_without_windows_pe(self):
        out = self.out("db.iso")
        self.assertEqual(self.ruiso("-i", self.iso, "-o", out, "-db"), 0)
        img, files = self.read_tree(out)
        self.assertNotIn("autounattend.xml", files)
        text = files["sources/$oem$/$$/panther/unattend.xml"].read().decode()
        self.assertIn("<ProtectYourPC>3</ProtectYourPC>", text)
        self.assertIn("PreventDeviceEncryption", text)
        self.assertGreater(files["sources/appraiserres.dll"].size, 0)
        img.close()

    def test_all_xml_options(self):
        out = self.out("all.iso")
        rc = self.ruiso("-i", self.iso, "-o", out, "-tmrdbqs", "-u", "Alex", "--no-setup-wrapper",
                        "--timezone", "America/Chicago", "--locale", "en-GB", "--keyboard", "00000809",
                        "--verify")
        self.assertEqual(rc, 0)
        img, files = self.read_tree(out)
        text = files["autounattend.xml"].read().decode()
        doc = xml.dom.minidom.parseString(text)
        self.assertEqual(len(doc.getElementsByTagName("settings")), 3)
        for needle in ("<Name>Alex</Name>", "logonpasswordchg", "SkuSiPolicy.p7b",
                       "&amp;&amp;", "HiberbootEnabled", "OneDriveSetup.exe",
                       "<TimeZone>Central Standard Time</TimeZone>", "<UserLocale>en-GB</UserLocale>",
                       "<InputLocale>0809:00000809</InputLocale>", "<UILanguage>en-US</UILanguage>",
                       "TCGSecurityActivationDisabled"):
            self.assertIn(needle, text)
        img.close()

    def test_qol_alone_keeps_first_logon_commands(self):
        text = unattend.build({"qol"}, "amd64")
        self.assertIn("FirstLogonCommands", text)
        self.assertIn("Microsoft-Windows-Shell-Setup", text)

    def test_unsupported_options_are_dropped_for_win10(self):
        tree = make_tree(os.path.join(self.tmp, "w10"), build=19045)
        iso = self.out("w10.iso")
        build_iso(tree, iso)
        out = self.out("w10_out.iso")
        self.assertEqual(self.ruiso("-i", iso, "-o", out, "-tmd"), 0)
        img, files = self.read_tree(out)
        self.assertNotIn("autounattend.xml", files)   # -t and -m don't apply to Windows 10
        self.assertIn("sources/$oem$/$$/panther/unattend.xml", files)
        img.close()

    def test_refuses_to_overwrite(self):
        out = self.out("exists.iso")
        open(out, "wb").close()
        self.assertEqual(self.ruiso("-i", self.iso, "-o", out, "-d"), 1)
        self.assertEqual(self.ruiso("-i", self.iso, "-o", out, "-d", "-f"), 0)
        self.assertEqual(self.ruiso("-i", self.iso, "-o", self.iso, "-d", "-f"), 1)

    def test_username_rules(self):
        self.assertEqual(unattend.sanitize_username("Alex")[0], "Alex")
        self.assertIsNone(unattend.sanitize_username("administrator")[0])
        self.assertEqual(unattend.sanitize_username("a&b<c")[0], "a_b_c")
        self.assertEqual(len(unattend.sanitize_username("x" * 40)[0]), 20)

    def test_command_line_roundtrip(self):
        sel = {"tpm", "msa", "user", "qol"}
        cmd = cli.command_line("in.iso", "out.iso", sel, "Alex")
        self.assertEqual(cmd, "ruiso -i in.iso -o out.iso -tmq -u Alex")
        args = cli.build_parser().parse_args(cmd.split()[1:])
        got = {o.key for o in OPTIONS if o.key != "user" and getattr(args, o.key)} | {"user"}
        self.assertEqual(got, sel)


class TestLargeFile(unittest.TestCase):
    """install.wim over 4 GB must survive (needs ~5 GB of free disk space)."""

    @unittest.skipUnless(os.environ.get("RUISO_BIG_TEST"), "set RUISO_BIG_TEST=1 to run")
    def test_big_install_wim(self):
        tmp = tempfile.mkdtemp(prefix="ruiso-big-")
        try:
            size = 5 * 1024 ** 3 + 12345
            tree = make_tree(os.path.join(tmp, "tree"), big_install=size)
            iso = os.path.join(tmp, "big.iso")
            build_iso(tree, iso)
            out = os.path.join(tmp, "big_out.iso")
            self.assertEqual(cli.main(["-i", iso, "-o", out, "-tm", "--no-setup-wrapper"]), 0)
            img = isofs.IsoImage(out)
            node = img.root.find("sources/install.wim")
            self.assertEqual(node.size, size)
            self.assertEqual(len(node.source[2]), 6)       # split into 1 GB extents
            info = wim.detect_windows(img.root)            # XML sits at the very end
            self.assertEqual(info.build, 26200)
            img.close()
            if sys.platform == "darwin":
                mnt = os.path.join(tmp, "mnt")
                os.makedirs(mnt)
                subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mnt, out],
                               check=True, stdout=subprocess.DEVNULL)
                try:
                    self.assertEqual(os.path.getsize(os.path.join(mnt, "sources", "install.wim")), size)
                    with open(os.path.join(mnt, "sources", "install.wim"), "rb") as f:
                        f.seek(size - 200)
                        tail = f.read()
                    with open(os.path.join(tree, "sources", "install.wim"), "rb") as f:
                        f.seek(size - 200)
                        self.assertEqual(tail, f.read())
                finally:
                    subprocess.run(["hdiutil", "detach", mnt, "-quiet"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
