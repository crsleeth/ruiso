"""A terminal version of Rufus' "Windows User Experience" dialog (curses)."""
from __future__ import annotations

import curses
import locale
import os
import textwrap

from .options import OPTIONS
from .unattend import MAX_USERNAME, USERNAME_INVALID_CHARS

FOOTER = "↑↓ move  Space toggle  Tab edit name  Enter OK  Esc cancel  Underlined letter = flag"
FOOTER_SHORT = "↑↓ Space Tab Enter Esc  ·  underlined letters toggle options"


class DialogContext:
    def __init__(self, *, input_path: str, output_path: str, image_desc: str, available: dict,
                 selected: set[str], username: str, regional_summary=None, command=None):
        self.input_path = input_path
        self.output_path = output_path
        self.image_desc = image_desc
        self.available = available            # key -> None or reason unavailable
        self.selected = set(selected)
        self.username = username
        self.regional_summary = regional_summary  # callable -> str
        self.command = command                    # callable(selected, username) -> str


def run(ctx: DialogContext):
    """Show the dialog. Returns (selected keys, username) or None if cancelled."""
    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        return curses.wrapper(lambda scr: _Dialog(scr, ctx).loop())
    except KeyboardInterrupt:
        return None


class _Dialog:
    def __init__(self, scr, ctx: DialogContext):
        self.scr, self.ctx = scr, ctx
        self.items = [o for o in OPTIONS if ctx.available.get(o.key) is None]
        self.hidden = [o for o in OPTIONS if ctx.available.get(o.key) is not None]
        self.sel = {k for k in ctx.selected if k in {o.key for o in self.items}}
        self.name = ctx.username or ""
        self.focus = 0                 # 0..n-1 options, n = OK, n+1 = Cancel
        self.editing = False
        self.cursor = len(self.name)
        self.rows: dict[int, int] = {}  # screen row -> focus index
        self.field_x = None
        self.button_x = {}
        self._regional = None
        self.message = ""

        curses.curs_set(0)
        scr.keypad(True)
        self.accent = self.dim = curses.A_NORMAL
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_CYAN, bg)
            curses.init_pair(2, curses.COLOR_GREEN, bg)
            curses.init_pair(3, curses.COLOR_YELLOW, bg)
            self.accent = curses.color_pair(1) | curses.A_BOLD
            self.check = curses.color_pair(2) | curses.A_BOLD
            self.warn = curses.color_pair(3)
        else:
            self.check = curses.A_BOLD
            self.warn = curses.A_BOLD
        self.dim = curses.A_DIM
        try:
            curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
            curses.mouseinterval(0)
        except curses.error:
            pass

    @property
    def n(self) -> int:
        return len(self.items)

    # ------------------------------------------------------------------ #
    def loop(self):
        while True:
            self.draw()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            if ch == "\x1b":
                ch = self.escape_sequence()
                if ch is None:
                    continue
            result = self.handle(ch)
            if result is not None:
                self.scr.erase()          # leave a clean screen even without an alternate screen
                self.scr.refresh()
                return None if result == "cancel" else (set(self.sel), self.name.strip())

    def escape_sequence(self):
        """Arrow keys that curses didn't decode (terminal not in keypad mode, tmux, ssh…)."""
        seq = ""
        self.scr.nodelay(True)
        try:
            while len(seq) < 6:
                try:
                    c = self.scr.get_wch()
                except curses.error:
                    break
                if not isinstance(c, str):
                    break
                seq += c
                if c.isalpha() or c == "~":
                    break
        finally:
            self.scr.nodelay(False)
        if not seq:
            return "\x1b"  # a real Esc key press
        return {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN,
                "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT, "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT,
                "[H": curses.KEY_HOME, "OH": curses.KEY_HOME, "[F": curses.KEY_END, "OF": curses.KEY_END,
                "[1~": curses.KEY_HOME, "[4~": curses.KEY_END, "[3~": curses.KEY_DC,
                "[Z": curses.KEY_BTAB, "OM": "\n"}.get(seq)

    def handle(self, ch):
        if ch == curses.KEY_RESIZE:
            return None
        if ch == curses.KEY_MOUSE:
            return self.mouse()
        if self.editing:
            return self.edit_key(ch)
        if ch in (curses.KEY_UP, "k"):
            self.focus = max(0, self.focus - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.focus = min(self.n + 1, self.focus + 1)
        elif ch in (curses.KEY_HOME,):
            self.focus = 0
        elif ch in (curses.KEY_END,):
            self.focus = self.n
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t", curses.KEY_BTAB):
            if self.focus >= self.n:
                self.focus = self.n if self.focus == self.n + 1 else self.n + 1
            elif self.items[self.focus].key == "user" and ch in ("\t", curses.KEY_RIGHT):
                self.start_edit()
            elif ch == "\t":
                self.focus = self.n
        elif ch == " ":
            if self.focus < self.n:
                self.toggle(self.items[self.focus])
            else:
                return "ok" if self.focus == self.n else "cancel"
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            return "cancel" if self.focus == self.n + 1 else "ok"
        elif ch in ("\x1b", "\x03"):
            return "cancel"
        elif isinstance(ch, str) and len(ch) == 1:
            for i, o in enumerate(self.items):
                if o.short == ch.lower():
                    self.focus = i
                    self.toggle(o)
                    if o.key == "user" and o.key in self.sel:
                        self.start_edit()
                    break
            else:
                curses.beep()
        return None

    def toggle(self, o):
        if o.key in self.sel:
            self.sel.discard(o.key)
        else:
            self.sel.add(o.key)
        self.message = ""

    def start_edit(self):
        self.editing = True
        self.cursor = len(self.name)

    def edit_key(self, ch):
        if ch in ("\n", "\r", "\t", "\x1b", curses.KEY_ENTER):
            self.editing = False
        elif ch in (curses.KEY_UP, curses.KEY_DOWN):
            self.editing = False
            return self.handle(ch)
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor > 0:
                self.name = self.name[:self.cursor - 1] + self.name[self.cursor:]
                self.cursor -= 1
        elif ch == curses.KEY_DC:
            self.name = self.name[:self.cursor] + self.name[self.cursor + 1:]
        elif ch == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif ch == curses.KEY_RIGHT:
            self.cursor = min(len(self.name), self.cursor + 1)
        elif ch in (curses.KEY_HOME, "\x01"):
            self.cursor = 0
        elif ch in (curses.KEY_END, "\x05"):
            self.cursor = len(self.name)
        elif ch == "\x15":  # Ctrl-U
            self.name, self.cursor = "", 0
        elif isinstance(ch, str) and ch.isprintable():
            if ch in USERNAME_INVALID_CHARS:
                self.message = f"'{ch}' isn't allowed in a Windows user name"
                curses.beep()
            elif len(self.name) >= MAX_USERNAME:
                self.message = f"Windows user names are limited to {MAX_USERNAME} characters"
                curses.beep()
            else:
                self.name = self.name[:self.cursor] + ch + self.name[self.cursor:]
                self.cursor += 1
                self.sel.add("user")
                self.message = ""
        return None

    def mouse(self):
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return None
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return None
        idx = self.rows.get(y)
        if idx is None:
            return None
        if idx >= self.n:
            for b, (x0, x1) in self.button_x.items():
                if x0 <= x < x1:
                    return "ok" if b == self.n else "cancel"
            return None
        self.editing = False
        self.focus = idx
        o = self.items[idx]
        if o.key == "user" and self.field_x is not None and x >= self.field_x:
            self.sel.add("user")
            self.start_edit()
        else:
            self.toggle(o)
        return None

    # ------------------------------------------------------------------ #
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def regional_text(self) -> str:
        if self._regional is None and self.ctx.regional_summary:
            try:
                self._regional = self.ctx.regional_summary()
            except Exception as e:  # never let detection crash the dialog
                self._regional = f"(could not detect: {e})"
        return self._regional or ""

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.rows.clear()
        self.button_x.clear()
        base = self.n + 3                               # title + options + buttons + footer
        if h < base or w < 40:
            self.put(0, 0, f"Terminal too small ({w}x{h}); need at least 40x{base}.", self.warn)
            self.put(1, 0, "Resize the window, or press Esc to cancel.")
            scr.refresh()
            return

        # Spend the spare rows on the most useful extras first, so 80x24 still looks right.
        budget = h - base
        show = {}
        for name, cost in (("tip2", 2), ("cmd", 1), ("sep", 1), ("heading", 1), ("output", 1),
                           ("input", 1), ("tip3", 1), ("desc", 1), ("hidden", 1 if self.hidden else 0),
                           ("gap4", 1), ("gap1", 1), ("gap2", 1), ("gap3", 1), ("tip4", 1)):
            show[name] = budget >= cost and cost > 0
            if show[name]:
                budget -= cost
        tip_rows = 2 * show["tip2"] + show["tip3"] + show["tip4"]

        y = 0
        self.put(y, 1, " ruiso ", self.accent | curses.A_REVERSE)
        self.put(y, 9, "Windows User Experience", curses.A_BOLD)
        y += 1 + show["gap1"]
        if show["input"]:
            self.put(y, 2, "Input", self.dim)
            self.put(y, 9, _middle(self.ctx.input_path, w - 11))
            y += 1
        if show["desc"]:
            self.put(y, 9, _middle(self.ctx.image_desc, w - 11), self.dim)
            y += 1
        if show["output"]:
            self.put(y, 2, "Output", self.dim)
            self.put(y, 9, _middle(self.ctx.output_path, w - 11))
            y += 1
        y += show["gap2"]
        if show["heading"]:
            self.put(y, 2, "Customize Windows installation?", curses.A_BOLD)
            y += 1
        y += show["gap3"]

        flags_col = w - 4 if w >= 90 else None
        x0 = 6
        self.field_x = None
        for i, o in enumerate(self.items):
            self.rows[y] = i
            focused = i == self.focus and not (self.editing and o.key == "user")
            on = o.key in self.sel
            self.put(y, 2, "[x]" if on else "[ ]", self.check if on else 0)
            avail = (flags_col - 1 if flags_col else w - 1) - x0
            if o.key == "user":
                avail -= MAX_USERNAME + 3
            text = _clip(o.label, max(8, avail))
            attr = curses.A_REVERSE if focused else 0
            self.put(y, x0, text, attr)
            if 0 <= o.hotkey_at < len(text) - 1:
                self.put(y, x0 + o.hotkey_at, text[o.hotkey_at], attr | curses.A_UNDERLINE | curses.A_BOLD)
            if o.key == "user":
                fx = x0 + len(text) + 1
                self.field_x = fx
                self.put(y, fx, "[", self.dim)
                self.put(y, fx + 1, self.name.ljust(MAX_USERNAME),
                         curses.A_UNDERLINE | (curses.A_BOLD if self.editing else 0))
                self.put(y, fx + 1 + MAX_USERNAME, "]", self.dim)
            if flags_col:
                self.put(y, flags_col, f"-{o.short}", self.dim)
            y += 1
        if show["hidden"]:
            keys = ", ".join(f"-{o.short}" for o in self.hidden)
            reasons = "; ".join(sorted({self.ctx.available[o.key] for o in self.hidden}))
            self.put(y, x0, _clip(f"Not offered for this image: {keys} ({reasons})", w - x0 - 1), self.dim)
            y += 1
        y += show["gap4"]

        ok, cancel = "   OK   ", " Cancel "
        bx = max(2, (w - len(ok) - len(cancel) - 8) // 2)
        self.rows[y] = self.n
        for idx, label, x in ((self.n, ok, bx), (self.n + 1, cancel, bx + len(ok) + 6)):
            attr = (curses.A_REVERSE | curses.A_BOLD) if self.focus == idx else curses.A_BOLD
            self.put(y, x, f"[{label}]", attr)
            self.button_x[idx] = (x, x + len(label) + 2)
        y += 1

        if show["sep"]:
            self.put(y, 0, "─" * w, self.dim)
            y += 1
        tips: list[tuple[str, int]] = []
        if self.message:
            tips.append((self.message, self.warn))
        elif self.focus < self.n:
            o = self.items[self.focus]
            tips += [(t, self.dim) for t in textwrap.wrap(o.tooltip, max(20, w - 4))]
            if o.key == "regional":
                tips += [(t, 0) for t in textwrap.wrap("This computer: " + self.regional_text(), max(20, w - 4))]
        if len(tips) > tip_rows and tip_rows:
            # keep the regional preview visible; shorten the tooltip instead
            keep = [t for t in tips if t[1] == 0][: tip_rows]
            head = [t for t in tips if t[1] != 0][: tip_rows - len(keep)]
            if head and len(head) < len([t for t in tips if t[1] != 0]):
                head[-1] = (_clip(head[-1][0] + " …", w - 4), head[-1][1])
            tips = head + keep
        for text, attr in tips[:tip_rows]:
            self.put(y, 2, text, attr)
            y += 1

        if show["cmd"] and self.ctx.command:
            self.put(h - 2, 2, _clip("$ " + self.ctx.command(self.sel, self.name), w - 4), self.accent)
        footer = FOOTER if w >= len(FOOTER) + 2 else FOOTER_SHORT
        self.put(h - 1, 0, _clip(" " + footer, w - 1).ljust(w - 1), curses.A_REVERSE)

        if self.editing and self.field_x is not None:
            row = next(r for r, i in self.rows.items() if i < self.n and self.items[i].key == "user")
            try:
                curses.curs_set(1)
                scr.move(row, self.field_x + 1 + self.cursor)
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        scr.refresh()


def _clip(s: str, n: int) -> str:
    if n <= 1:
        return s[:max(0, n)]
    return s if len(s) <= n else s[: n - 1] + "…"


def _middle(s: str, n: int) -> str:
    if len(s) <= n or n < 10:
        return s[:max(0, n)]
    half = (n - 1) // 2
    return s[:half] + "…" + s[-(n - 1 - half):]
