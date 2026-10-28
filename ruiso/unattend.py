"""Generate the Windows answer file, mirroring Rufus' CreateUnattendXml() (src/wue.c)."""
from __future__ import annotations

from xml.sax.saxutils import escape

# Same lists as Rufus
UNALLOWED_ACCOUNT_NAMES = [
    "Administrator", "Järjestelmänvalvoja", "Administrateur", "Rendszergazda", "Administrador",
    "Администратор", "Administratör", "Guest", "DefaultAccount", "WDAGUtilityAccount",
    "HelpAssistant", "KRBTGT", "Local", "NONE", "SYSTEM",
]
USERNAME_INVALID_CHARS = '/\\[]:;|=.,+*?<>%@&"'
MAX_USERNAME = 20  # Windows limit for local account names

BYPASS_NAMES = ["BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"]

ROOT_PATH = "autounattend.xml"
PANTHER_PATH = "sources/$OEM$/$$/Panther/unattend.xml"


def sanitize_username(name: str) -> tuple[str | None, list[str]]:
    """Return (usable name or None, warnings)."""
    warnings = []
    name = (name or "").strip()
    if not name:
        return None, warnings
    if name.lower() in (n.lower() for n in UNALLOWED_ACCOUNT_NAMES):
        return None, [f"'{name}' is not allowed as a local account name - option ignored"]
    clean = "".join("_" if c in USERNAME_INVALID_CHARS else c for c in name)
    if clean != name:
        warnings.append("local account name contained characters Windows doesn't allow; "
                        f"using '{clean}'")
    if len(clean) > MAX_USERNAME:
        clean = clean[:MAX_USERNAME]
        warnings.append(f"local account names are limited to {MAX_USERNAME} characters; using '{clean}'")
    return clean, warnings


def answer_file_path(selected: set[str]) -> str:
    """Where the answer file goes on the media.

    Rufus puts it inside boot.wim when it has a windowsPE section and in
    \\sources\\$OEM$\\$$\\Panther otherwise (an Autounattend.xml without a windowsPE
    pass isn't carried over to the installed system). For an ISO the root of the
    disc is the standard place for the windowsPE case.
    """
    return ROOT_PATH if "tpm" in selected else PANTHER_PATH


def _component(name: str, arch: str) -> str:
    return (f'    <component name="{name}" processorArchitecture="{arch}" language="neutral" '
            'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">')


def build(selected: set[str], arch: str = "amd64", username: str | None = None,
          regional=None) -> str | None:
    """Return the answer file text, or None if the selection needs no answer file."""
    arch = arch if arch in ("x86", "amd64", "arm", "arm64") else "amd64"
    sel = set(selected)
    if "user" in sel and not username:
        sel.discard("user")
    if "regional" in sel and regional is None:
        sel.discard("regional")

    L: list[str] = []
    w = L.append
    w('<?xml version="1.0" encoding="utf-8"?>')
    w('<unattend xmlns="urn:schemas-microsoft-com:unattend">')
    body_start = len(L)

    # ---- windowsPE: hardware requirement bypass ----
    if "tpm" in sel:
        w('  <settings pass="windowsPE">')
        w(_component("Microsoft-Windows-Setup", arch))
        # WinPE complains if no product key is provided. *Any* product key.
        w("      <UserData>")
        w("        <AcceptEula>true</AcceptEula>")
        w("        <ProductKey>")
        w("          <Key />")
        w("        </ProductKey>")
        w("      </UserData>")
        w("      <RunSynchronous>")
        for i, name in enumerate(BYPASS_NAMES, 1):
            w('        <RunSynchronousCommand wcm:action="add">')
            w(f"          <Order>{i}</Order>")
            w(f"          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {name} /t REG_DWORD /d 1 /f</Path>")
            w("        </RunSynchronousCommand>")
        w("      </RunSynchronous>")
        w("    </component>")
        w("  </settings>")

    # ---- specialize: online account bypass + QoL ----
    if sel & {"msa", "qol"}:
        cmds = []
        if "msa" in sel:
            cmds.append('reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\OOBE" '
                        '/v BypassNRO /t REG_DWORD /d 1 /f')
        if "qol" in sel:
            cmds += _QOL_SPECIALIZE
        w('  <settings pass="specialize">')
        w(_component("Microsoft-Windows-Deployment", arch))
        _commands(w, "RunSynchronous", "RunSynchronousCommand", "Path", cmds)
        w("    </component>")
        w("  </settings>")

    # ---- oobeSystem ----
    shell = sel & {"data", "user", "regional", "skusi", "qol"}
    if shell or sel & {"regional", "bitlocker"}:
        w('  <settings pass="oobeSystem">')
        if shell:
            # Rufus 4.15 only emits this component when data/user/regional is also
            # selected, which silently drops the QoL/SkuSiPolicy logon commands
            # otherwise. We always emit it when one of them needs it.
            w(_component("Microsoft-Windows-Shell-Setup", arch))
            if "data" in sel:
                w("      <OOBE>")
                w("        <HideEULAPage>true</HideEULAPage>")
                w("        <ProtectYourPC>3</ProtectYourPC>")
                w("      </OOBE>")
            if "regional" in sel and regional.timezone:
                w(f"      <TimeZone>{escape(regional.timezone)}</TimeZone>")
            cmds = []
            if "user" in sel:
                u = escape(username)
                w("      <UserAccounts>")
                w("        <LocalAccounts>")
                w('          <LocalAccount wcm:action="add">')
                w(f"            <Name>{u}</Name>")
                w(f"            <DisplayName>{u}</DisplayName>")
                w("            <Group>Administrators;Power Users</Group>")
                # Empty password ("Password" as Base64 UTF-16, Microsoft's convention);
                # the user is asked to set one at first logon.
                w("            <Password>")
                w("              <Value>UABhAHMAcwB3AG8AcgBkAA==</Value>")
                w("              <PlainText>false</PlainText>")
                w("            </Password>")
                w("          </LocalAccount>")
                w("        </LocalAccounts>")
                w("      </UserAccounts>")
                cmds.append(f'net user "{username}" /logonpasswordchg:yes')
                cmds.append("net accounts /maxpwage:unlimited")
            if "skusi" in sel:
                cmds.append("cmd /c mountvol S: /S && "
                            "copy %WINDIR%\\system32\\SecureBootUpdates\\SkuSiPolicy.p7b S:\\EFI\\Microsoft\\Boot && "
                            "mountvol S: /D")
            if "qol" in sel:
                cmds += _QOL_FIRSTLOGON
            _commands(w, "FirstLogonCommands", "SynchronousCommand", "CommandLine", cmds)
            w("    </component>")
        if "regional" in sel:
            w(_component("Microsoft-Windows-International-Core", arch))
            w(f"      <InputLocale>{escape(regional.input_locale)}</InputLocale>")
            w(f"      <SystemLocale>{escape(regional.system_locale)}</SystemLocale>")
            w(f"      <UserLocale>{escape(regional.user_locale)}</UserLocale>")
            w(f"      <UILanguage>{escape(regional.ui_language)}</UILanguage>")
            w(f"      <UILanguageFallback>{escape(regional.ui_fallback)}</UILanguageFallback>")
            w("    </component>")
        if "bitlocker" in sel:
            w(_component("Microsoft-Windows-SecureStartup-FilterDriver", arch))
            w("      <PreventDeviceEncryption>true</PreventDeviceEncryption>")
            w("    </component>")
            w(_component("Microsoft-Windows-EnhancedStorage-Adm", arch))
            w("      <TCGSecurityActivationDisabled>1</TCGSecurityActivationDisabled>")
            w("    </component>")
        w("  </settings>")

    if len(L) == body_start:
        return None
    w("</unattend>")
    return "\r\n".join(L) + "\r\n"


def _commands(w, section: str, item: str, field: str, cmds: list[str]):
    if not cmds:
        return
    w(f"      <{section}>")
    for i, c in enumerate(cmds, 1):
        w(f'        <{item} wcm:action="add">')
        w(f"          <Order>{i}</Order>")
        w(f"          <{field}>{escape(c)}</{field}>")
        w(f"        </{item}>")
    w(f"      </{section}>")


_PS = "PowerShell -NonInteractive -WindowStyle Hidden -Command "

# specialize pass: remove OneDrive, Outlook and Teams
_QOL_SPECIALIZE = [
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\OneDrive" /v DisableFileSyncNGSC /t REG_DWORD /d 1 /f',
    _PS + '"Remove-Item -Path $env:SystemRoot\\System32\\OneDriveSetup.exe -Force -Confirm:$false; '
          'Remove-Item -Path $env:SystemRoot\\SysWOW64\\OneDriveSetup.exe -Force -Confirm:$false;"',
    _PS + "\"Get-AppxProvisionedPackage -Online | Where-Object {$_.PackageName -like '*Outlook*'} |"
          " Remove-AppxProvisionedPackage -Online\"",
    _PS + '"Get-AppxPackage -AllUsers *Outlook* | Remove-AppxPackage -AllUsers"',
    _PS + "\"Get-AppxProvisionedPackage -Online | Where-Object {$_.PackageName -like '*Teams*'} |"
          " Remove-AppxProvisionedPackage -Online\"",
    _PS + '"Get-AppxPackage -AllUsers *Teams* | Remove-AppxPackage -AllUsers"',
]

# First logon: Fast Startup, Copilot, ads, news, Teams, Outlook pin, Edge, Start menu, context menu
_QOL_FIRSTLOGON = [
    'reg add "HKLM\\System\\CurrentControlSet\\Control\\Session Manager\\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f',
    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowCopilotButton /t REG_DWORD /d 0 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\WindowsCopilot" /v TurnOffWindowsCopilot /t REG_DWORD /d 1 /f',
    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Search" /v SearchboxTaskbarMode /t REG_DWORD /d 1 /f',
    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Search" /v SearchboxTaskbarModeCache /t REG_DWORD /d 1 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v DisableWindowsConsumerFeatures /t REG_DWORD /d 1 /f',
    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager" /v SystemPaneSuggestionsEnabled /t REG_DWORD /d 0 /f',
    'reg add "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search" /v BingSearchEnabled /t REG_DWORD /d 0 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\Device Metadata" /v PreventDeviceMetadataFromNetwork /t REG_DWORD /d 1 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Dsh" /v AllowNewsAndInterests /t REG_DWORD /d 0 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\Windows Feeds" /v EnableFeeds /t REG_DWORD /d 0 /f',
    'reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Communications" /v ConfigureChatAutoInstall /t REG_DWORD /d 0 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v DisableCloudOptimizedContent /t REG_DWORD /d 1 /f',
    'reg add "HKLM\\Software\\Policies\\Microsoft\\Edge" /v HideFirstRunExperience /t REG_DWORD /d 1 /f',
    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v Start_Layout /t REG_DWORD /d 1 /f',
    _PS + "\"Set-ItemProperty -Path 'Registry::HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Start' "
          "-Name 'VisiblePlaces' -Value $([convert]::FromBase64String('ztU0LVr6Q0WC8iLm6vd3PC+zZ+PeiVVDv85h83sYqTe8JIo"
          "UDNaJQqCAbtm7okiCRIF1/g0IrkKL2jTtl7ZjlEqwvXRK+WhPi9ZDmAcdqLyGCHNSqlFDQp97J3ZYRlnU')) -Type 'Binary'\"",
    'reg add "HKCU\\Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32" /ve /t REG_SZ /d "" /f',
]
