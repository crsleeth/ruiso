# ruiso

[Rufus](https://rufus.ie)' "Windows User Experience" options (skip the TPM and Microsoft
account checks, create a local user, and so on), applied to a Windows 10/11 ISO and
written to a new bootable ISO. Use the Rufus-style TUI or one command.

## Why

Rufus only applies these options while writing a USB drive, and only runs on Windows.
`ruiso` runs anywhere Python does and gives you the same result as an ISO file, so you
can:

- Install Windows 11 in a VM (UTM, Parallels, VMware, VirtualBox, Proxmox, Hyper-V)
  without a virtual TPM, Secure Boot or 4 GB of RAM, and without a Microsoft account.
- Drop the ISO on a Ventoy drive, or write it with any USB tool, and still get Rufus'
  tweaks.
- Keep one ready-made ISO for repeat installs instead of clicking through setup each
  time.

## Install

Needs Python 3.8+ and nothing else. From a clone of this repo:

```bash
./install.sh
```

The `-c` option also needs wimlib or 7-Zip to read `boot.wim`.

## Use

```bash
ruiso -i Win11.iso                  # pick options in the TUI
ruiso -i Win11.iso -o out.iso -tm   # same as Rufus' defaults, no TUI
```

| Flag | Rufus option |
|---|---|
| `-t` | Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0 |
| `-m` | Remove requirement for an online Microsoft account (keep the PC offline during setup) |
| `-u NAME` | Create a local account with username NAME |
| `-r` | Set regional options to the same values as this user's |
| `-d` | Disable data collection (Skip privacy questions) |
| `-b` | Disable BitLocker automatic device encryption |
| `-q` | QoL improvements (Don't force Copilot, OneDrive, Outlook, Fast Startup, etc.) |
| `-c` | Use 'Windows CA 2023' signed bootloaders (Requires a compatible target PC) |
| `-s` | Apply SkuSiPolicy.p7b on installation (See KB5042562) |

Flags combine (`-tmdbq`), and any flag skips the TUI. The output defaults to
`<input>_ruiso.iso`. Run `ruiso --help` for the rest.

## How it works

- The options become a Windows answer file, generated the same way as in Rufus.
- With `-t`, the in-place upgrade tweaks are applied too, as in Rufus. One of them is a
  small `setup.exe` wrapper downloaded from Rufus' GitHub and checked by SHA-256; skip it
  with `--no-setup-wrapper`.
- The ISO is read and written directly, with no mounting or root access, and keeps
  Microsoft's layout (UDF, BIOS + UEFI boot).

## License

GPLv3. The option behavior and wording come from [Rufus](https://github.com/pbatard/rufus)
by Pete Batard.
