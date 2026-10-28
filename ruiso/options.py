"""The Windows User Experience options, in the same order and wording as Rufus."""
from __future__ import annotations


class Option:
    def __init__(self, key: str, short: str, long: str, label: str, tooltip: str, summary: str,
                 hotkey_word: str = ""):
        self.key = key          # internal name
        self.short = short      # single-letter flag, also the TUI hotkey
        self.long = long        # long flag
        self.label = label      # exact Rufus wording
        self.tooltip = tooltip  # Rufus tooltip
        self.summary = summary  # short line for logs
        # The TUI underlines the first letter of this word: it's the option's flag/hotkey.
        self.hotkey_at = label.find(hotkey_word) if hotkey_word else -1


OPTIONS = [
    Option("tpm", "t", "no-tpm",
           "Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0",
           "It is safe to leave this option enabled even if you have a TPM or more RAM, as this "
           "option only bypasses the setup requirements and does not actually prevent Windows "
           "from using all the hardware available.",
           "Bypass Secure Boot / TPM / RAM checks", "TPM 2.0"),
    Option("msa", "m", "no-msa",
           "Remove requirement for an online Microsoft account",
           "For this option to work, network/Internet MUST be disconnected during installation!",
           "Bypass online account requirement", "Microsoft"),
    Option("user", "u", "user",
           "Create a local account with username:",
           "Automatically create a local account with the specified user name, using an empty "
           "password that will need to be changed on next logon.",
           "Create local account", "username"),
    Option("regional", "r", "regional",
           "Set regional options to the same values as this user's",
           "Duplicate the regional settings from this computer (keyboard, timezone, currency), "
           "instead of prompting the user.",
           "Use this computer's regional options", "regional"),
    Option("data", "d", "no-data-collection",
           "Disable data collection (Skip privacy questions)",
           "Automatically answer 'no' to the Windows setup questions relating to the sharing of "
           "data with Microsoft, instead of prompting the user.",
           "Disable data collection", "data collection"),
    Option("bitlocker", "b", "no-bitlocker",
           "Disable BitLocker automatic device encryption",
           "Don't encrypt the system disk, unless explicitly requested by the user.",
           "Disable BitLocker", "BitLocker"),
    Option("qol", "q", "qol",
           "QoL improvements (Don't force Copilot, OneDrive, Outlook, Fast Startup, etc.)",
           "\"Quality of Life\" enhancements: Disable most of the unwanted features Microsoft is "
           "trying to force onto end users.",
           "QoL improvements", "QoL"),
    Option("ca2023", "c", "ca2023",
           "Use 'Windows CA 2023' signed bootloaders (Requires a compatible target PC)",
           "Use this option if the system you plan to install Windows on is using fully up-to-date "
           "Secure Boot certificates. If needed, you can look into using 'Mosby' to update your system.",
           "Use 'Windows CA 2023' signed bootloaders", "CA 2023"),
    Option("skusi", "s", "skusipolicy",
           "Apply SkuSiPolicy.p7b on installation (See KB5042562)",
           "Use this option if you want to revoke additional potentially unsafe Windows bootloaders, "
           "but with the potential of also preventing standard Windows media from booting.",
           "Apply SkuSiPolicy.p7b", "SkuSiPolicy"),
]

BY_KEY = {o.key: o for o in OPTIONS}
BY_SHORT = {o.short: o for o in OPTIONS}

# Rufus pre-selects these the first time the dialog is shown.
DEFAULT_SELECTION = {"tpm", "msa"}


def availability(info) -> dict[str, str | None]:
    """Map option key -> None if usable, else the reason it isn't (same rules as Rufus)."""
    out = {o.key: None for o in OPTIONS}
    if info is None or not info.is_windows or not info.build:
        return out  # unknown image: offer everything
    win11 = "needs Windows 11"
    if not info.is_win11:
        for k in ("tpm", "bitlocker", "qol"):
            out[k] = win11
    if info.build < 22500:
        out["msa"] = "needs Windows 11 build 22500+"
    if not info.is_win11 or info.build < 26200:
        out["ca2023"] = out["skusi"] = "needs Windows 11 build 26200+ (25H2)"
    return out
