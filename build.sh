#!/bin/bash
# AudioTagger build script
# Запускай из папки где лежит audio_tagger.py:
#   chmod +x build.sh && ./build.sh

set -e

APP_NAME="AudioTagger"
SCRIPT="audio_tagger.py"
VENV="$HOME/lyrics-env"
DMG_NAME="${APP_NAME}.dmg"
VERSION=$(cat version.txt 2>/dev/null || echo "v1.0")
PYTHON="$VENV/bin/python3"
PIP="$VENV/bin/pip"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

build_log()  { echo -e "${GREEN}▶ $1${NC}"; }
build_warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
build_fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

build_log "Building version: $VERSION"

# Проверяем что скрипт есть
[ -f "$SCRIPT" ] || build_fail "$SCRIPT not found — run from the folder with audio_tagger.py"

# Проверяем venv
[ -f "$PYTHON" ] || build_fail "venv not found at $VENV. Run:
  /opt/homebrew/opt/python@3.13/bin/python3.13 -m venv ~/lyrics-env
  source ~/lyrics-env/bin/activate
  pip install mutagen lyricsgenius tqdm requests numpy soundfile customtkinter pillow pyinstaller"

build_log "Using Python: $($PYTHON --version)"

# Проверяем tkinter
$PYTHON -c "import tkinter" 2>/dev/null || true  # tkinter optional now

# Проверяем и устанавливаем зависимости
build_log "Checking dependencies..."
$PIP install pyinstaller pillow pywebview customtkinter mutagen lyricsgenius tqdm requests numpy soundfile onnxruntime termcolor -q

# Проверяем pywebview
$PYTHON -c "import webview" || build_fail "pywebview not importable — run: pip install pywebview"
build_log "All dependencies OK"

# Homebrew + create-dmg
if ! command -v create-dmg &>/dev/null; then
    build_log "Installing create-dmg via Homebrew..."
    command -v brew &>/dev/null || build_fail "Homebrew not found. Install from https://brew.sh"
    brew install create-dmg -q
fi

# Создаём config.txt если нет
if [ ! -f "config.txt" ]; then
    cat > config.txt << 'CFG'
GENIUS_TOKEN=YOUR_GENIUS_API_TOKEN
LASTFM_KEY=YOUR_LASTFM_API_KEY
MUSIC_ROOT=
LOG_PATH=
CFG
    build_warn "Created config.txt — fill in your tokens after installing"
fi

# ---- ИКОНКА ----
build_log "Generating icon..."
rm -rf icon.icns icon.iconset
mkdir -p icon.iconset

$PYTHON << 'PYEOF'
from PIL import Image, ImageDraw
import os

def make_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = int(size * 0.22)
    draw.rounded_rectangle([0, 0, size-1, size-1], radius=r, fill=(28, 28, 30, 255))
    heights_ratio = [0.20, 0.38, 0.28, 0.50, 0.28, 0.38, 0.20]
    n = len(heights_ratio)
    total_w = size * 0.75
    bar_w = total_w / (n * 2 - 1)
    gap = bar_w
    start_x = (size - total_w) / 2
    for i, h_ratio in enumerate(heights_ratio):
        h = int(size * h_ratio)
        x0 = int(start_x + i * (bar_w + gap))
        x1 = int(x0 + bar_w)
        y0 = (size - h) // 2
        y1 = y0 + h
        color = (50, 215, 75, 255) if i == 3 else (10, 132, 255, 255)
        rad = max(2, (x1 - x0) // 2)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=color)
    return img

sizes = [16, 32, 64, 128, 256, 512, 1024]
for s in sizes:
    img = make_icon(s)
    img.save(f'icon.iconset/icon_{s}x{s}.png')
    if s <= 512:
        make_icon(s * 2).save(f'icon.iconset/icon_{s}x{s}@2x.png')
print("Icons generated")
PYEOF

iconutil -c icns icon.iconset -o icon.icns
rm -rf icon.iconset
build_log "Icon created: icon.icns"

# ---- PYINSTALLER ----
# ---- DOWNLOAD rsgain binary ----
build_log "Downloading rsgain..."
RSGAIN_VERSION="v3.6"
RSGAIN_URL="https://github.com/complexlogic/rsgain/releases/download/v3.6/rsgain-3.6-macOS-arm64.zip"
mkdir -p ./bin
if [ ! -f "./bin/rsgain" ]; then
    curl -L "$RSGAIN_URL" -o /tmp/rsgain.zip
    mkdir -p /tmp/rsgain_extracted
    unzip -o /tmp/rsgain.zip -d /tmp/rsgain_extracted/
    find /tmp/rsgain_extracted -name "rsgain" -type f | head -1 | xargs -I{} cp {} ./bin/rsgain
    chmod +x ./bin/rsgain
    rm -rf /tmp/rsgain.zip /tmp/rsgain_extracted
    build_log "rsgain ready: $(./bin/rsgain --version 2>&1 | head -1)"
else
    build_log "rsgain already present"
fi

# ---- COPY ffmpeg from Homebrew (if available) ----
build_log "Checking ffmpeg..."
if [ -f "/opt/homebrew/bin/ffmpeg" ]; then
    rm -f ./bin/ffmpeg
    cp /opt/homebrew/bin/ffmpeg ./bin/
    chmod +x ./bin/ffmpeg
    build_log "ffmpeg bundled from Homebrew"
else
    build_warn "ffmpeg not found — M4A DR analysis will be unavailable without Homebrew"
fi

# ---- BUNDLE FLAD ----
rm -rf ./flad_bundle  # clean stale bundle before rebuild
build_log "Bundling FLAD..."
FLAD_SRC="$HOME/FLAD"
mkdir -p ./flad_bundle/models
mkdir -p ./flad_bundle/flad

# Always create flad_bundle dir — PyInstaller requires it to exist
mkdir -p ./flad_bundle/flad ./flad_bundle/models
touch ./flad_bundle/__init__.py
touch ./flad_bundle/flad/__init__.py
echo "# FLAD placeholder" > ./flad_bundle/flad/placeholder.py

# Find model — it may be in $FLAD_SRC/models/ or $FLAD_SRC/flad/models/
FLAD_MODEL=""
[ -f "$FLAD_SRC/models/flad.onnx" ] && FLAD_MODEL="$FLAD_SRC/models/flad.onnx"
[ -f "$FLAD_SRC/flad/models/flad.onnx" ] && FLAD_MODEL="$FLAD_SRC/flad/models/flad.onnx"

if [ -d "$FLAD_SRC/flad" ] && [ -n "$FLAD_MODEL" ]; then
    cp -r "$FLAD_SRC/flad/"* ./flad_bundle/flad/
    cp "$FLAD_MODEL" ./flad_bundle/models/flad.onnx
    touch ./flad_bundle/flad/__init__.py
    # Copy top-level utils/eval if present
    for f in utils.py utils1.py eval.py eval1.py; do
        [ -f "$FLAD_SRC/$f" ] && cp "$FLAD_SRC/$f" ./flad_bundle/ 2>/dev/null || true
    done
    build_log "FLAD bundled (model: $(du -sh ./flad_bundle/models/flad.onnx | cut -f1))"
else
    build_warn "FLAD not found at $FLAD_SRC — bundle created without FLAD model"
    build_warn "FLAC source analysis will be unavailable until FLAD is installed via Settings"
fi

build_log "Building .app with PyInstaller..."
rm -rf build dist  # clean stale build artifacts

$VENV/bin/pyinstaller \
    --windowed \
    --name "$APP_NAME" \
    --icon "icon.icns" \
    --add-data "config.txt:." \
    --osx-bundle-identifier "com.ngrishaev.audiotagger" \
    --hidden-import "webview" \
    --hidden-import "webview.platforms.cocoa" \
    --hidden-import "mutagen" \
    --hidden-import "lyricsgenius" \
    --hidden-import "requests" \
    --hidden-import "numpy" \
    --hidden-import "soundfile" \
    --hidden-import "onnxruntime" \
    --hidden-import "PIL" \
    --hidden-import "PIL.Image" \
    --hidden-import "termcolor" \
    --hidden-import "cffi" \
    --collect-all "onnxruntime" \
    --collect-all "webview" \
    --collect-all "mutagen" \
    --collect-all "lyricsgenius" \
    --collect-all "soundfile" \
    --add-binary "./bin/rsgain:bin" \
    --add-binary "./bin/ffmpeg:bin" \
    --add-data "./flad_bundle:flad_bundle" \
    --add-data "./version.txt:." \
    --copy-metadata "mutagen" \
    --copy-metadata "requests" \
    --clean \
    -y \
    "$SCRIPT"

build_log ".app built: dist/${APP_NAME}.app"

# ---- ПОДПИСЬ ----
build_log "Signing app (ad-hoc)..."
codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null && \
    build_log "Signed OK" || build_warn "Signing failed — app may show Gatekeeper warning"
xattr -cr "dist/${APP_NAME}.app" 2>/dev/null || true

# ---- DMG ----
build_log "Building DMG..."
rm -f "$DMG_NAME"
DMG_SRC=$(mktemp -d)
cp -r "dist/${APP_NAME}.app" "$DMG_SRC/"

# Build DMG with icon but skip AppleScript (avoids macOS Finder hang)
create-dmg \
    --volname "$APP_NAME Installer" \
    --volicon "icon.icns" \
    --window-pos 200 120 \
    --window-size 560 380 \
    --icon-size 120 \
    --icon "${APP_NAME}.app" 140 190 \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link 420 190 \
    --no-internet-enable \
    --skip-jenkins \
    "$DMG_NAME" \
    "$DMG_SRC"

rm -rf "$DMG_SRC"

# Set DMG volume icon via osascript with timeout
# This runs after DMG is built so a hang won't break the build
build_log "Setting DMG volume icon..."
# Convert to read-write, set icon, convert back to read-only compressed
RW_DMG="${DMG_NAME%.dmg}-rw.dmg"
hdiutil convert "$DMG_NAME" -format UDRW -o "$RW_DMG" -quiet 2>/dev/null
if [ -f "$RW_DMG" ]; then
    MOUNT_POINT=$(hdiutil attach "$RW_DMG" -nobrowse -noautoopen 2>/dev/null | awk '/\/Volumes/{for(i=3;i<=NF;i++) printf "%s%s",$i,(i<NF?" ":""); print ""}')
    if [ -n "$MOUNT_POINT" ]; then
        cp "icon.icns" "${MOUNT_POINT}/.VolumeIcon.icns" 2>/dev/null || true
        SetFile -a C "${MOUNT_POINT}" 2>/dev/null || true
        hdiutil detach "${MOUNT_POINT}" -quiet 2>/dev/null || true
        # Convert back to compressed read-only
        rm -f "$DMG_NAME"
        hdiutil convert "$RW_DMG" -format UDZO -o "$DMG_NAME" -quiet 2>/dev/null
        build_log "Volume icon set"
    else
        build_warn "Could not mount RW DMG — keeping original"
    fi
    rm -f "$RW_DMG"
else
    build_warn "Could not convert DMG to RW — skipping icon"
fi

SIZE=$(du -sh "$DMG_NAME" | cut -f1)
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  Build complete!                  ║${NC}"
echo -e "${GREEN}║                                      ║${NC}"
echo -e "${GREEN}║  📦 ${DMG_NAME} (${SIZE})          ║${NC}"
echo -e "${GREEN}║                                      ║${NC}"
echo -e "${GREEN}║  open ${DMG_NAME}                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"