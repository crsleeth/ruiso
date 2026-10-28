#!/bin/sh
# Put a `ruiso` command on your PATH that runs this checkout.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$here/bin/ruiso"
for dir in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin; do
  case ":$PATH:" in
    *":$dir:"*)
      if [ -d "$dir" ] && [ -w "$dir" ]; then
        ln -sf "$here/bin/ruiso" "$dir/ruiso"
        echo "Installed: $dir/ruiso -> $here/bin/ruiso"
        exit 0
      fi ;;
  esac
done
mkdir -p "$HOME/.local/bin"
ln -sf "$here/bin/ruiso" "$HOME/.local/bin/ruiso"
echo "Installed: $HOME/.local/bin/ruiso"
echo "Add it to your PATH, e.g.:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
