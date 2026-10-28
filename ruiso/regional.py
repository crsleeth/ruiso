"""Work out this computer's regional settings in Windows terms.

Rufus copies the Windows host's keyboard, locale, display language and time
zone. We do the same from a macOS or Linux host, translating to the names
Windows expects (e.g. America/Chicago -> "Central Standard Time").
"""
from __future__ import annotations

import locale as _locale
import os
import re
import subprocess
import sys

from . import tzdata

# macOS keyboard layout (com.apple.keylayout.<name>) -> Windows keyboard layout ID
APPLE_KEYBOARDS = {
    "US": "00000409", "ABC": "00000409", "USExtended": "00000409", "ABC-Extended": "00000409",
    "USInternational-PC": "00020409", "Dvorak": "00010409", "DVORAK-QWERTYCMD": "00010409",
    "Australian": "00000409", "Canadian": "00000409", "Canadian-CSA": "00011009",
    "CanadianFrench-PC": "00001009", "British": "00000809", "British-PC": "00000809",
    "Irish": "00001809", "IrishExtended": "00001809",
    "German": "00000407", "Austrian": "00000407", "SwissGerman": "00000807",
    "French": "0000040C", "French-PC": "0000040C", "French-numerical": "0000040C",
    "SwissFrench": "0000100C", "Belgian": "0000080C",
    "Spanish": "0000040A", "Spanish-ISO": "0000040A", "LatinAmerican": "0000080A",
    "Italian": "00000410", "Italian-Pro": "00000410",
    "Portuguese": "00000816", "Brazilian": "00000416", "Brazilian-ABNT2": "00000416",
    "Brazilian-Pro": "00000416", "Dutch": "00020409",
    "Danish": "00000406", "Norwegian": "00000414", "NorwegianExtended": "00000414",
    "Swedish": "0000041D", "Swedish-Pro": "0000041D", "Finnish": "0000040B",
    "FinnishExtended": "0000040B", "Icelandic": "0000040F",
    "Polish": "00000415", "PolishPro": "00000415", "Czech": "00000405", "Czech-QWERTY": "00010405",
    "Slovak": "0000041B", "Slovak-QWERTY": "0001041B", "Hungarian": "0000040E",
    "Hungarian-QWERTY": "0001040E", "Romanian": "00010418", "Romanian-Standard": "00010418",
    "Croatian": "0000041A", "Croatian-PC": "0000041A", "Slovenian": "00000424",
    "Serbian-Latin": "0000081A", "Russian": "00000419", "RussianWin": "00000419",
    "Russian-Phonetic": "00000419", "Ukrainian": "00000422", "Ukrainian-PC": "00020422",
    "Bulgarian": "00030402", "Greek": "00000408", "GreekPolytonic": "00000408",
    "Turkish": "0001041F", "Turkish-QWERTY": "0000041F", "Turkish-QWERTY-PC": "0000041F",
    "Hebrew": "0000040D", "Hebrew-PC": "0000040D", "Arabic": "00000401", "Arabic-PC": "00000401",
    "Estonian": "00000425", "Latvian": "00000426", "Lithuanian": "00010427",
    "Thai": "0000041E", "Vietnamese": "0000042A",
}

# Linux/X11 keyboard layout (layout or layout+variant) -> Windows keyboard layout ID
XKB_KEYBOARDS = {
    "us": "00000409", "us+intl": "00020409", "us+altgr-intl": "00020409", "us+dvorak": "00010409",
    "gb": "00000809", "ie": "00001809", "de": "00000407", "at": "00000407", "ch": "00000807",
    "ch+fr": "0000100C", "fr": "0000040C", "be": "0000080C", "ca": "00001009",
    "ca+multix": "00011009", "es": "0000040A", "latam": "0000080A", "it": "00000410",
    "pt": "00000816", "br": "00000416", "nl": "00020409", "dk": "00000406", "no": "00000414",
    "se": "0000041D", "fi": "0000040B", "is": "0000040F", "pl": "00000415", "cz": "00000405",
    "cz+qwerty": "00010405", "sk": "0000041B", "sk+qwerty": "0001041B", "hu": "0000040E",
    "ro": "00010418", "hr": "0000041A", "si": "00000424", "rs+latin": "0000081A",
    "ru": "00000419", "ua": "00000422", "bg": "00030402", "gr": "00000408", "tr": "0000041F",
    "tr+f": "0001041F", "il": "0000040D", "ara": "00000401", "ee": "00000425", "lv": "00000426",
    "lt": "00010427", "th": "0000041E", "vn": "0000042A",
}

# Windows locale name -> LCID (from Python's own table)
_LCID = {}
for _lcid, _name in getattr(_locale, "windows_locale", {}).items():
    _LCID.setdefault(_name.replace("_", "-").lower(), _lcid)


class Regional:
    def __init__(self):
        self.input_locale = "en-US"
        self.system_locale = "en-US"
        self.user_locale = "en-US"
        self.ui_language = "en-US"
        self.ui_fallback = "en-US"
        self.timezone: str | None = None
        self.keyboard_name: str | None = None
        self.notes: list[str] = []

    def summary(self) -> str:
        kb = self.keyboard_name or self.input_locale
        tz = self.timezone or "time zone not set"
        return f"{self.user_locale} formats · {kb} keyboard · {self.ui_language} display · {tz}"


def to_tag(value: str | None) -> str | None:
    """Turn 'en_US.UTF-8', 'en_US@rg=gbzzzz', 'zh-Hans_CN' etc. into a Windows locale name."""
    if not value:
        return None
    rg = re.search(r"@rg=([a-z]{2})", value, re.I)
    t = value.split(".")[0].split("@")[0].replace("_", "-").strip()
    if t in ("", "C", "POSIX"):
        return None
    parts = t.split("-")
    lang, script, region = parts[0].lower(), None, None
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            script = p.title()
        elif (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            region = p.upper()
    if rg:
        region = rg.group(1).upper()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        return None
    if lang == "zh":
        if script == "Hant" or region in ("TW", "HK", "MO"):
            return "zh-" + (region if region in ("TW", "HK", "MO") else "TW")
        return "zh-" + (region if region in ("CN", "SG") else "CN")
    if lang == "sr":
        return f"sr-{script or 'Latn'}-{region or 'RS'}"
    return f"{lang}-{region}" if region else lang


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _host_timezone() -> str | None:
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone") as f:
            return f.read().strip() or None
    except OSError:
        pass
    out = _run(["timedatectl", "show", "-p", "Timezone", "--value"]).strip()
    return out or None


def _mac_locale() -> tuple[str | None, str | None, str | None, str | None]:
    """(format locale, UI language, keyboard ID, keyboard name) on macOS."""
    fmt = to_tag(_run(["defaults", "read", "-g", "AppleLocale"]).strip())
    langs = re.findall(r'"?([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)"?\s*,?\s*$',
                       _run(["defaults", "read", "-g", "AppleLanguages"]), re.M)
    ui = to_tag(langs[0]) if langs else None
    if ui and "-" not in ui and fmt and fmt.split("-")[0] == ui:
        ui = fmt
    name = _mac_keyboard(_run(["defaults", "read", "com.apple.HIToolbox"]))
    return fmt, ui, APPLE_KEYBOARDS.get(name) if name else None, name


def _mac_keyboard(hitoolbox: str) -> str | None:
    """Current keyboard layout name (as in com.apple.keylayout.<name>) from HIToolbox prefs."""
    m = re.search(r'AppleCurrentKeyboardLayoutInputSourceID = "?com\.apple\.keylayout\.([\w-]+)', hitoolbox)
    if m:
        return m.group(1)
    saved = re.search(r"AppleSavedCurrentInputSource = +\{(.*?)\}", hitoolbox, re.S)
    for text in ((saved.group(1),) if saved else ()) + (hitoolbox,):
        m = re.search(r'"KeyboardLayout Name" = "?([^";\n]+)"?;', text)
        if m:
            name = m.group(1).strip().replace("U.S.", "US").replace(" - ", "-").replace(" ", "")
            return name
        m = re.search(r"com\.apple\.keylayout\.([\w-]+)", text)
        if m:
            return m.group(1)
    return None


def _linux_locale() -> tuple[str | None, str | None, str | None, str | None]:
    env = os.environ
    fmt = to_tag(env.get("LC_ALL") or env.get("LC_TIME") or env.get("LANG"))
    ui = to_tag(env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG"))
    status = _run(["localectl", "status"])
    if not fmt:
        m = re.search(r"LANG=(\S+)", status)
        fmt = ui = to_tag(m.group(1)) if m else None
    layout = variant = None
    m = re.search(r"X11 Layout:\s*(\S+)", status)
    if m:
        layout = m.group(1).split(",")[0]
        mv = re.search(r"X11 Variant:\s*(\S+)", status)
        variant = mv.group(1).split(",")[0] if mv else None
    else:
        try:
            with open("/etc/default/keyboard") as f:
                text = f.read()
            ml = re.search(r'XKBLAYOUT="?([^",\n]+)', text)
            mv = re.search(r'XKBVARIANT="?([^",\n]+)', text)
            layout = ml.group(1) if ml else None
            variant = mv.group(1) if mv else None
        except OSError:
            pass
    klid = None
    if layout:
        klid = XKB_KEYBOARDS.get(f"{layout}+{variant}") if variant else None
        klid = klid or XKB_KEYBOARDS.get(layout)
    name = f"{layout}{'+' + variant if variant else ''}" if layout else None
    return fmt, ui, klid, name


def _input_locale(user_locale: str, klid: str | None) -> str:
    if not klid:
        return user_locale          # Windows picks that language's default keyboard
    lcid = _LCID.get(user_locale.lower())
    k = int(klid, 16)
    if lcid is not None and (lcid & 0x3FF) == (k & 0x3FF):
        return f"{lcid:04x}:{klid}"
    return f"{k & 0xFFFF:04x}:{klid}"


def _pick(tag: str, available: list[str]) -> str | None:
    for a in available:
        if a.lower() == tag.lower():
            return a
    for a in available:
        if a.split("-")[0].lower() == tag.split("-")[0].lower():
            return a
    return None


def detect(iso_languages: list[str] | None = None, *, locale: str | None = None,
           ui_language: str | None = None, keyboard: str | None = None,
           timezone: str | None = None) -> Regional:
    """Detect host settings; keyword arguments override what is detected."""
    r = Regional()
    if sys.platform == "darwin":
        fmt, ui, klid, kb_name = _mac_locale()
    else:
        fmt, ui, klid, kb_name = _linux_locale()

    fmt = to_tag(locale) or fmt or "en-US"
    if "-" not in fmt:
        fmt = {"en": "en-US"}.get(fmt, f"{fmt}-{fmt.upper()}")
    r.system_locale = r.user_locale = fmt

    ui = to_tag(ui_language) or ui or fmt
    langs = [l for l in (iso_languages or []) if l]
    if langs and not ui_language:
        chosen = _pick(ui, langs)
        if chosen is None:
            r.notes.append(f"display language {ui} isn't in this ISO; using {langs[0]}")
            chosen = langs[0]
        ui = chosen
    r.ui_language = r.ui_fallback = ui

    if keyboard:
        k = keyboard.strip()
        if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F{}\-]+", k):
            r.input_locale, r.keyboard_name = k, k
        elif re.fullmatch(r"[0-9a-fA-F]{8}", k):
            r.input_locale, r.keyboard_name = _input_locale(fmt, k.upper()), k
        else:
            tag = to_tag(k) or k
            r.input_locale, r.keyboard_name = tag, tag
    else:
        r.input_locale = _input_locale(fmt, klid)
        r.keyboard_name = kb_name if klid else None
        if kb_name and not klid:
            r.notes.append(f"keyboard '{kb_name}' has no known Windows equivalent; "
                           f"Windows will use the default {fmt} keyboard")

    iana = timezone or _host_timezone()
    win_tz = tzdata.normalize_windows_zone(iana) if iana else None
    if win_tz is None:
        r.notes.append(f"couldn't map time zone '{iana}' to Windows; Windows will ask" if iana
                       else "couldn't detect the time zone; Windows will ask")
    r.timezone = win_tz
    return r
