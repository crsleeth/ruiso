"""ruiso command line: Rufus' Windows User Experience options, written to a new ISO."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shlex
import shutil
import sys
import time

from . import __version__, isofs, isowriter, unattend, wim
from . import regional as regional_mod
from .customize import Customizer
from .options import BY_KEY, DEFAULT_SELECTION, OPTIONS, availability

EXAMPLES = """\
examples:
  ruiso -i Win11_25H2_English_x64.iso                pick options in the TUI (like Rufus)
  ruiso -i Win11.iso -o Win11_custom.iso -tm         Rufus' defaults, no TUI
  ruiso -i Win11.iso -o out.iso -tmdbq -r -u Alex    more options, local account "Alex"
  ruiso -i Win11.iso -tm --print-xml                 only show the answer file

With no option flags (and no -y) the TUI opens so you can tick options like in
Rufus. Any option flag skips the TUI; add --tui to open it pre-filled instead.
"""


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

class Style:
    def __init__(self, stream):
        on = stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.bold = "\033[1m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.green = "\033[32m" if on else ""
        self.yellow = "\033[33m" if on else ""
        self.red = "\033[31m" if on else ""
        self.cyan = "\033[36m" if on else ""
        self.reset = "\033[0m" if on else ""


S = Style(sys.stdout)


_OUT = None  # set to sys.stderr when stdout carries data (--print-xml)


def say(text: str = ""):
    print(text, file=_OUT or sys.stdout, flush=True)


def warn(text: str):
    print(f"{S.yellow}warning:{S.reset} {text}", file=sys.stderr, flush=True)


class Fail(Exception):
    pass


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_time(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 60}:{s % 60:02d}"


class Progress:
    def __init__(self, label: str = "Writing"):
        self.label = label
        self.t0 = time.time()
        self.last = 0.0
        self.tty = sys.stdout.isatty()
        self.next_pct = 10

    def __call__(self, done: int, total: int):
        now = time.time()
        frac = done / total if total else 1.0
        if self.tty:
            if now - self.last < 0.1 and done < total:
                return
            self.last = now
            width = 28
            fill = int(frac * width)
            rate = done / max(1e-6, now - self.t0)
            eta = (total - done) / rate if rate > 0 else 0
            line = (f"\r{self.label}  {S.cyan}{'█' * fill}{S.dim}{'░' * (width - fill)}{S.reset} "
                    f"{frac * 100:5.1f}%  {fmt_size(done)} / {fmt_size(total)}  "
                    f"{fmt_size(rate)}/s  ETA {fmt_time(eta)} ")
            sys.stdout.write(line)
            sys.stdout.flush()
        elif frac * 100 >= self.next_pct:
            say(f"{self.label} {int(frac * 100)}%")
            self.next_pct = (int(frac * 100) // 10 + 1) * 10

    def done(self):
        if self.tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Settings (the TUI remembers your last choices, like Rufus)
# --------------------------------------------------------------------------- #

def _settings_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ruiso", "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(selected: set[str], username: str):
    try:
        path = _settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"options": sorted(selected), "username": username}, f, indent=2)
    except OSError:
        pass


def default_username() -> str:
    name = ""
    try:
        import pwd
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        name = gecos.split()[0] if gecos else ""
    except (ImportError, KeyError, IndexError):
        pass
    name = name or getpass.getuser()
    clean, _ = unattend.sanitize_username(name)
    return clean or ""


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ruiso", add_help=False, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="ruiso -i WINDOWS.iso [-o NEW.iso] [-tmrdbqcs] [-u NAME] [options]",
        description="Apply Rufus' \"Windows User Experience\" options to a Windows ISO "
                    "and write the result to a new ISO.",
        epilog=EXAMPLES)
    io = p.add_argument_group("input / output")
    io.add_argument("-i", "--input", metavar="WINDOWS.iso",
                    help="Windows 10/11 ISO (or a folder with an extracted ISO)")
    io.add_argument("-o", "--output", metavar="NEW.iso",
                    help="ISO to create (default: <input>_ruiso.iso next to the input)")
    io.add_argument("paths", nargs="*", help=argparse.SUPPRESS)

    w = p.add_argument_group("Windows User Experience options (same as Rufus)")
    for o in OPTIONS:
        if o.key == "user":
            w.add_argument("-u", "--user", metavar="NAME", help="Create a local account with username NAME")
        else:
            w.add_argument(f"-{o.short}", f"--{o.long}", dest=o.key, action="store_true", help=o.label)

    r = p.add_argument_group("regional overrides (used with -r)")
    r.add_argument("--locale", metavar="TAG", help="formats and system locale, e.g. en-GB")
    r.add_argument("--ui-language", metavar="TAG", help="Windows display language (must be in the ISO)")
    r.add_argument("--keyboard", metavar="ID", help="keyboard: 0409:00000409, 00000809 or a locale like de-DE")
    r.add_argument("--timezone", metavar="ZONE", help='"Pacific Standard Time" or America/Los_Angeles')

    g = p.add_argument_group("other")
    g.add_argument("-y", "--yes", action="store_true",
                   help="don't open the TUI; with no option flags, use Rufus' defaults (-tm)")
    g.add_argument("--tui", action="store_true", help="open the TUI even when option flags are given")
    g.add_argument("-f", "--force", action="store_true", help="overwrite the output ISO if it exists")
    g.add_argument("-n", "--dry-run", action="store_true", help="show what would change, write nothing")
    g.add_argument("--print-xml", action="store_true", help="print the generated answer file and exit")
    g.add_argument("--verify", action="store_true", help="re-read the new ISO and compare every file")
    g.add_argument("--no-setup-wrapper", action="store_true",
                   help="with -t: skip Rufus' setup.exe wrapper for in-place upgrades "
                        "(fetched once from Rufus' GitHub, SHA-256 checked)")
    g.add_argument("-h", "--help", action="help", help="show this help and exit")
    g.add_argument("-V", "--version", action="version", version=f"ruiso {__version__}")
    return p


def command_line(input_arg: str, output_arg: str | None, selected: set[str], username: str) -> str:
    parts = ["ruiso", "-i", shlex.quote(input_arg)]
    if output_arg:
        parts += ["-o", shlex.quote(output_arg)]
    flags = "".join(o.short for o in OPTIONS if o.key in selected and o.key != "user")
    if flags:
        parts.append("-" + flags)
    if "user" in selected and username.strip():
        parts += ["-u", shlex.quote(username.strip())]
    if not selected:
        parts.append("-y")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args, parser)
    except Fail as e:
        print(f"{S.red}error:{S.reset} {e}", file=sys.stderr)
        return 1
    except (isofs.IsoError, RuntimeError, OSError) as e:
        print(f"{S.red}error:{S.reset} {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


def run(args, parser) -> int:
    images: list = []
    try:
        return _run(args, parser, images)
    finally:
        for img in images:
            img.close()


def _run(args, parser, images: list) -> int:
    global _OUT
    _OUT = sys.stderr if args.print_xml else None
    paths = list(args.paths)
    if paths and not args.input:
        args.input = paths.pop(0)
    if paths and not args.output:
        args.output = paths.pop(0)
    if paths:
        parser.error(f"unexpected argument: {paths[0]}")
    if not args.input:
        parser.print_usage(sys.stderr)
        raise Fail("no input ISO given (use -i WINDOWS.iso)")
    if not os.path.exists(args.input):
        raise Fail(f"input not found: {args.input}")

    output = args.output or _default_output(args.input)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # ---- read the source image ----
    img = None
    if os.path.isdir(args.input):
        root = isofs.tree_from_directory(args.input)
        boot = isofs.default_boot_entries(root)
        udf_ids, iso_pvd, readme, validation_id = {}, None, None, b""
    else:
        img = isofs.IsoImage(args.input)
        images.append(img)
        root, boot = img.root, img.boot.entries
        udf_ids, iso_pvd, readme = img.udf_ids, img.pvd, img.iso_readme
        validation_id = img.boot.validation_id
    info = wim.detect_windows(root)
    avail = availability(info)

    say(f"{S.bold}ruiso {__version__}{S.reset}")
    say(f"  Input   {args.input}")
    say(f"          {S.dim}{info.describe()}{S.reset}")
    if not info.is_windows:
        if not args.force:
            raise Fail("this doesn't look like Windows 10/11 install media "
                       "(no sources/install.wim or boot.wim). Use -f to continue anyway.")
        warn("not recognised as Windows install media; continuing because of -f")
    elif info.major and info.major < 10:
        warn("Rufus only offers these options for Windows 10 and 11 images")
    if not boot:
        warn("the input has no boot records, so the new ISO won't be bootable")

    if not args.dry_run and not args.print_xml:
        _check_output(args.input, output)

    # ---- choose options ----
    flag_sel = {o.key for o in OPTIONS if o.key != "user" and getattr(args, o.key)}
    if args.user is not None:
        flag_sel.add("user")
    settings = load_settings()
    use_tui = args.tui or (not flag_sel and not args.yes)
    username = args.user if args.user is not None else (settings.get("username") or default_username())
    overrides = dict(locale=args.locale, ui_language=args.ui_language, keyboard=args.keyboard,
                     timezone=args.timezone)

    if use_tui:
        if not interactive:
            raise Fail("no options given and no terminal for the TUI. Pass option flags "
                       "(e.g. -tm) or -y for Rufus' defaults.")
        from . import tui
        initial = flag_sel or set(settings.get("options") or DEFAULT_SELECTION)
        ctx = tui.DialogContext(
            input_path=args.input, output_path=output, image_desc=info.describe(),
            available=avail, selected=initial, username=username,
            regional_summary=lambda: regional_mod.detect(info.languages, **overrides).summary(),
            command=lambda sel, name: command_line(args.input, args.output, sel, name))
        result = tui.run(ctx)
        if result is None:
            say("Cancelled.")
            return 130
        selected, username = result
        save_settings(selected, username)
        say(f"  {S.dim}Same as: {command_line(args.input, args.output, selected, username)}{S.reset}")
    else:
        selected = set(flag_sel) if flag_sel else (set(DEFAULT_SELECTION) if args.yes else set())

    for key in sorted(selected, key=lambda k: [o.key for o in OPTIONS].index(k)):
        if avail.get(key):
            warn(f"-{BY_KEY[key].short} ({BY_KEY[key].summary}) ignored: {avail[key]}")
            selected.discard(key)

    # ---- resolve option details ----
    user = None
    if "user" in selected:
        user, notes = unattend.sanitize_username(username)
        for n in notes:
            warn(n)
        if user is None:
            if not notes:
                warn("no user name given; local account option ignored")
            selected.discard("user")
    regional = None
    if "regional" in selected:
        regional = regional_mod.detect(info.languages, **overrides)
        for n in regional.notes:
            warn(n)
    elif any(overrides.values()):
        warn("--locale/--ui-language/--keyboard/--timezone only apply with -r")

    xml = unattend.build(selected, info.arch or "amd64", user, regional)
    if args.print_xml:
        if xml is None:
            say("(the selected options don't need an answer file)")
        else:
            sys.stdout.write(xml.replace("\r\n", "\n"))
        return 0

    say(f"  Output  {output}")
    say()
    if selected:
        say(f"{S.bold}Options{S.reset}")
        for o in OPTIONS:
            if o.key in selected:
                extra = ""
                if o.key == "user":
                    extra = f" '{user}'"
                elif o.key == "regional":
                    extra = f": {regional.summary()}"
                say(f"  {S.green}✓{S.reset} {o.summary}{extra}")
    else:
        warn("no options selected; the new ISO will have the same contents as the input")

    # ---- apply ----
    with Customizer(root, info, boot, log=lambda m: say(f"  {S.dim}{m}{S.reset}")) as cz:
        cz.apply(selected, xml, setup_wrapper=not args.no_setup_wrapper)
        if cz.changes:
            say(f"{S.bold}Changes{S.reset}")
            for c in cz.changes:
                color = {"+": S.green, "~": S.cyan, "!": S.yellow}[c.kind]
                say(f"  {color}{c.kind}{S.reset} {c.text}")

        plan = isowriter.Plan(root, boot)
        total = plan.total_sectors * isofs.SECTOR
        if args.dry_run:
            say()
            say(f"Dry run: would write {fmt_size(total)} to {output}")
            return 0
        _check_space(output, total, args.force)

        say()
        t0 = time.time()
        prog = Progress()
        try:
            isowriter.write_image(root, output, boot, udf_ids=udf_ids, iso_pvd=iso_pvd, readme=readme,
                                  validation_id=validation_id, progress=prog)
        finally:
            prog.done()
        elapsed = time.time() - t0
        _check_result(output, root, boot, xml, selected, full=args.verify)
    say(f"{S.green}{S.bold}Done.{S.reset} Wrote {output} ({fmt_size(total)}) in {fmt_time(elapsed)}")
    if "msa" in selected:
        say(f"  {S.dim}Tip: keep the PC/VM offline during setup for the online-account bypass to work.{S.reset}")
    return 0


def _default_output(input_path: str) -> str:
    base = os.path.basename(os.path.normpath(input_path))
    stem = base[:-4] if base.lower().endswith(".iso") else base
    return os.path.join(os.path.dirname(os.path.abspath(input_path)), f"{stem}_ruiso.iso")


def _check_output(input_path: str, output: str):
    if os.path.isdir(output):
        raise Fail(f"output is a directory: {output}")
    if os.path.exists(output) and os.path.realpath(output) == os.path.realpath(input_path):
        raise Fail("output would overwrite the input ISO; pick another -o")
    parent = os.path.dirname(os.path.abspath(output))
    if not os.path.isdir(parent):
        raise Fail(f"output folder doesn't exist: {parent}")


def _confirm_overwrite(output: str, force: bool) -> None:
    if not os.path.exists(output) or force:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        ans = input(f"{output} exists. Overwrite? [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            return
        raise KeyboardInterrupt
    raise Fail(f"{output} already exists (use -f to overwrite)")


def _check_space(output: str, needed: int, force: bool):
    _confirm_overwrite(output, force)
    parent = os.path.dirname(os.path.abspath(output))
    free = shutil.disk_usage(parent).free
    if os.path.exists(output):
        free += os.path.getsize(output)
    if free < needed + (16 << 20):
        raise Fail(f"not enough free space in {parent}: need {fmt_size(needed)}, have {fmt_size(free)}")


def _check_result(output: str, root: isofs.Node, boot, xml, selected, full: bool):
    """Re-open the new ISO and make sure it has what we meant to write."""
    new = isofs.IsoImage(output)
    try:
        want = {p.lower(): n for p, n in root.walk() if not n.is_dir}
        got = {p.lower(): n for p, n in new.root.walk() if not n.is_dir}
        if new.fs != "udf" or set(want) != set(got):
            missing = sorted(set(want) - set(got))[:3]
            raise Fail(f"verification failed: file list differs {missing}")
        for p, n in want.items():
            if got[p].size != n.size:
                raise Fail(f"verification failed: size of {p} differs")
        if len(new.boot.entries) != len(boot):
            raise Fail("verification failed: boot records differ")
        if xml is not None:
            path = unattend.answer_file_path(selected).lower()
            if got[path].read() != xml.encode("utf-8"):
                raise Fail("verification failed: answer file differs")
        if full:
            prog = Progress("Verifying")
            total = sum(n.size for n in want.values())
            done = 0
            try:
                for p, n in want.items():
                    a, b = hashlib.sha256(), hashlib.sha256()
                    for chunk in n.iter_data():
                        a.update(chunk)
                    for chunk in got[p].iter_data():
                        b.update(chunk)
                        done += len(chunk)
                        prog(done, total)
                    if a.digest() != b.digest():
                        raise Fail(f"verification failed: contents of {p} differ")
            finally:
                prog.done()
            say(f"  {S.green}✓{S.reset} Verified {len(want)} files")
    finally:
        new.close()
