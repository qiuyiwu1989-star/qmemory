#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"

mkdir -p assets artifacts

if [[ ! -f assets/QMemory.icns ]]; then
  temp_dir="$(mktemp -d)"
  iconset="$temp_dir/QMemory.iconset"
  mkdir -p "$iconset"
  qlmanage -t -s 1024 -o "$temp_dir" assets/icon.svg >/dev/null
  source_png="$temp_dir/icon.svg.png"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$source_png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    double_size=$((size * 2))
    sips -z "$double_size" "$double_size" "$source_png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o assets/QMemory.icns
fi

.venv/bin/pyside6-deploy desktop_main.py -c pysidedeploy.spec -f
plist="artifacts/QMemory.app/Contents/Info.plist"
plutil -replace CFBundleDisplayName -string QMemory "$plist"
plutil -replace CFBundleName -string QMemory "$plist"
plutil -replace CFBundleIdentifier -string com.qiuyiwu.qmemory "$plist"
plutil -replace CFBundleShortVersionString -string 0.11.0 "$plist"
plutil -replace CFBundleVersion -string 12 "$plist"
codesign --force --deep --sign - artifacts/QMemory.app
codesign --verify --deep --strict artifacts/QMemory.app
