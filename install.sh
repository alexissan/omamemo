#!/usr/bin/env bash
# Install only this application; never invoke sudo or a package manager.
set -euo pipefail
source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
app_dir="$data_home/omamemo-app"
bin_dir="$HOME/.local/bin"
desktop_dir="$data_home/applications"
icon_dir="$data_home/icons/hicolor/scalable/apps"
if [[ "$data_home" != /* ]]; then
  printf '%s\n' 'XDG_DATA_HOME must be an absolute path.' >&2
  exit 1
fi

if [[ $(uname -s) != Linux ]]; then
  printf '%s\n' 'Omamemo targets Linux. Install on your Omarchy/Arch machine.' >&2
  exit 1
fi
if [[ ! -x /usr/bin/python ]]; then
  printf '%s\n' 'Missing /usr/bin/python. On Arch: sudo pacman -S python pyside6 qt6-multimedia qt6-wayland ffmpeg pipewire pipewire-audio' >&2
  exit 1
fi
/usr/bin/python - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Omamemo requires Python 3.11 or newer.")
try:
    from PySide6 import QtCore, QtGui, QtWidgets, QtMultimedia
except ImportError as exc:
    raise SystemExit(f"Missing system PySide6/QtMultimedia: {exc}\nOn Arch: sudo pacman -S pyside6 qt6-multimedia")
PY
for dependency in ffmpeg ffprobe pw-record; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Missing %s. On Arch: sudo pacman -S ffmpeg pipewire pipewire-audio\n' "$dependency" >&2
    exit 1
  fi
done
[[ -f "$source_dir/omamemo/app.py" ]] || { printf '%s\n' 'Incomplete source tree: omamemo/app.py is missing.' >&2; exit 1; }
# Refuse to overwrite an unrelated directory or symlink.
if [[ -L "$app_dir" || ( -e "$app_dir" && ! -f "$app_dir/.omamemo-owned" ) ]]; then
  printf 'Refusing to replace unrecognized app directory: %s\n' "$app_dir" >&2
  exit 1
fi
for destination in "$bin_dir/omamemo" "$desktop_dir/omamemo.desktop"; do
  if [[ -e "$destination" || -L "$destination" ]]; then
    if [[ -L "$destination" ]] || ! grep -qF 'Omamemo managed launcher' "$destination"; then
      printf 'Refusing to overwrite unrecognized launcher: %s\n' "$destination" >&2
      exit 1
    fi
  fi
done
if [[ -L "$icon_dir/omamemo.svg" || ( -e "$icon_dir/omamemo.svg" && ! -f "$app_dir/.omamemo-owned" ) ]]; then
  printf 'Refusing to overwrite an existing icon: %s\n' "$icon_dir/omamemo.svg" >&2
  exit 1
fi
mkdir -p -- "$data_home" "$bin_dir" "$desktop_dir" "$icon_dir"
staging=$(mktemp -d "$data_home/.omamemo-install.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
/usr/bin/python - "$source_dir" "$staging" <<'PY'
from pathlib import Path
import shutil, sys
source, staging = map(Path, sys.argv[1:])
shutil.copytree(source / "omamemo", staging / "omamemo", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for name in ("LICENSE", "README.md", "uninstall.sh"):
    shutil.copy2(source / name, staging / name)
(staging / ".omamemo-owned").write_text("Omamemo application files; recordings are stored separately.\n")
PY
if [[ -d "$app_dir" ]]; then rm -rf -- "$app_dir"; fi
mv -- "$staging" "$app_dir"
{
  printf '%s\n' '#!/usr/bin/env bash' '# Omamemo managed launcher' 'set -euo pipefail'
  printf 'export PYTHONPATH=%q\n' "$app_dir"
  printf '%s\n' 'exec /usr/bin/python -m omamemo "$@"'
} > "$bin_dir/omamemo"
chmod 755 "$bin_dir/omamemo" "$app_dir/uninstall.sh"
/usr/bin/python - "$source_dir/assets/omamemo.desktop" "$desktop_dir/omamemo.desktop" "$bin_dir/omamemo" <<'PY'
from pathlib import Path
import sys
source, destination, executable = sys.argv[1:]
# Desktop Exec is not a shell command. Quote reserved characters per its spec.
escaped = executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
text = Path(source).read_text().replace("Exec=omamemo\n", f'Exec="{escaped}"\n')
Path(destination).write_text(text)
PY
cp -- "$source_dir/assets/omamemo.svg" "$icon_dir/omamemo.svg"
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$desktop_dir" || true; fi
printf 'Installed Omamemo. Launch from your application menu or %s/omamemo\n' "$bin_dir"
printf 'Recordings and transcripts stay separate in %s/omamemo.\n' "$data_home"
