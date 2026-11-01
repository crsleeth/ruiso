#!/bin/sh
# Remove ruiso installed by install.sh, along with its settings and download cache.
set -e

prefix="${PREFIX:-$HOME/.local}"
dest="$prefix/share/ruiso"
link="$prefix/bin/ruiso"

if [ -L "$link" ]; then
  rm -f "$link" && echo "Removed $link"
fi
if [ -d "$dest" ]; then
  rm -rf "$dest" && echo "Removed $dest"
fi
for dir in "${XDG_CONFIG_HOME:-$HOME/.config}/ruiso" "${XDG_CACHE_HOME:-$HOME/.cache}/ruiso"; do
  if [ -d "$dir" ]; then
    rm -rf "$dir" && echo "Removed $dir"
  fi
done

# Take out the PATH line install.sh added, if any.
for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
  if grep -qs "added by ruiso install.sh" "$rc"; then
    grep -v "added by ruiso install.sh" "$rc" > "$rc.ruiso-tmp" && cat "$rc.ruiso-tmp" > "$rc" && rm -f "$rc.ruiso-tmp"
    echo "Removed the PATH line from $rc"
  fi
done

echo "ruiso is uninstalled."
