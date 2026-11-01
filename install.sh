#!/bin/sh
# Install ruiso for the current user:
#   program -> ~/.local/share/ruiso
#   command -> ~/.local/bin/ruiso
# Run it again from a newer download to update. The download can be deleted afterwards.
# Set PREFIX to install somewhere else (e.g. PREFIX=/usr/local, with sudo).
set -e

src="$(cd "$(dirname "$0")" && pwd)"
prefix="${PREFIX:-$HOME/.local}"
dest="$prefix/share/ruiso"
bindir="$prefix/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ruiso needs Python 3.8 or newer (python3 was not found)." >&2
  exit 1
fi
if [ ! -f "$src/ruiso/cli.py" ] || [ ! -f "$src/bin/ruiso" ]; then
  echo "Run install.sh from inside the ruiso download." >&2
  exit 1
fi

# Copy the program (replacing any older copy).
mkdir -p "$dest.new"
cp -R "$src/ruiso" "$src/bin" "$dest.new/"
cp "$src/LICENSE" "$src/README.md" "$src/uninstall.sh" "$dest.new/" 2>/dev/null || true
find "$dest.new" -name "__pycache__" -type d -prune -exec rm -rf {} +
chmod +x "$dest.new/bin/ruiso"
rm -rf "$dest"
mv "$dest.new" "$dest"

# Link the command.
mkdir -p "$bindir"
ln -sf "$dest/bin/ruiso" "$bindir/ruiso"

# Remove links left by older versions of this script, which pointed at the download folder.
for dir in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  link="$dir/ruiso"
  [ "$link" = "$bindir/ruiso" ] && continue
  if [ -L "$link" ] && [ -w "$dir" ]; then
    case "$(readlink "$link")" in
      */bin/ruiso) rm -f "$link" && echo "Removed old link: $link" ;;
    esac
  fi
done

echo "Installed ruiso to $dest"
echo "Command: $bindir/ruiso"

# Make sure the command can be found.
case ":$PATH:" in
  *":$bindir:"*) ;;
  *)
    case "$(basename "${SHELL:-sh}")" in
      zsh) rc="$HOME/.zshrc" ;;
      bash) if [ "$(uname)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
      *) rc="$HOME/.profile" ;;
    esac
    line="export PATH=\"$bindir:\$PATH\"  # added by ruiso install.sh"
    if ! grep -qs "added by ruiso install.sh" "$rc"; then
      printf '\n%s\n' "$line" >> "$rc"
      echo "Added $bindir to your PATH in $rc"
    fi
    echo "Open a new terminal window, then run: ruiso --help"
    ;;
esac
