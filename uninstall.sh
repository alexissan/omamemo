#!/usr/bin/env bash
set -euo pipefail
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
if [[ "$data_home" != /* ]]; then
  printf '%s\n' 'XDG_DATA_HOME must be an absolute path.' >&2
  exit 1
fi
app_dir="$data_home/omamemo-app"
launcher="$HOME/.local/bin/omamemo"
desktop="$data_home/applications/omamemo.desktop"
icon="$data_home/icons/hicolor/scalable/apps/omamemo.svg"
printf 'Uninstalling app files only. Recordings and transcripts are retained in %s/omamemo.\n' "$data_home"
if [[ -L "$app_dir" || ( -e "$app_dir" && ! -f "$app_dir/.omamemo-owned" ) ]]; then
  printf 'Refusing to remove unrecognized app directory: %s\n' "$app_dir" >&2
  exit 1
fi
for file in "$launcher" "$desktop"; do
  if [[ -f "$file" && ! -L "$file" ]] && grep -qF 'Omamemo managed launcher' "$file"; then
    rm -- "$file"
  elif [[ -e "$file" || -L "$file" ]]; then
    printf 'Leaving unrecognized launcher: %s\n' "$file" >&2
  fi
done
if [[ -f "$app_dir/.omamemo-owned" ]]; then
  rm -f -- "$icon"
  rm -rf -- "$app_dir"
fi
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$data_home/applications" || true; fi
printf '%s\n' 'Omamemo app removed. Your memo library has not been deleted.'
