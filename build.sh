#!/bin/bash
# AudioTagger build script
# Запускай из папки где лежит audio_tagger.py:
#   chmod +x build.sh && ./build.sh

set -e

APP_NAME="AudioTagger"
SCRIPT="audio_tagger.py"
VENV="$HOME/lyrics-env"
DMG_NAME="${APP_NAME}.dmg"
PYTHON="$VENV/bin/python3"
PIP="$VENV/bin/pip"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}▶ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# Проверяем что скрипт есть
[ -f "$SCRIPT" ] || fail "$SCRIPT not found — run from the folder with audio_tagger.py"

# Проверяем venv
[ -f "$PYTHON" ] || fail "venv not found at $VENV. Run:
  /opt/homebrew/opt/python@3.13/bin/python3.13 -m venv ~/lyrics-env
  source ~/lyrics-env/bin/activate
  pip install mutagen lyricsgenius tqdm requests numpy soundfile customtkinter pillow pyinstaller"

log "Using Python: $($PYTHON --version)"

# Проверяем tkinter
$PYTHON -c "import tkinter" 2>/dev/null || true  # tkinter optional now

# Проверяем и устанавливаем зависимости
log "Checking dependencies..."
$PIP install pyinstaller pillow pywebview customtkinter mutagen lyricsgenius tqdm requests numpy soundfile -q

# Проверяем pywebview
$PYTHON -c "import webview" || fail "pywebview not importable — run: pip install pywebview"
log "All dependencies OK"

# Homebrew + create-dmg
if ! command -v create-dmg &>/dev/null; then
    log "Installing create-dmg via Homebrew..."
    command -v brew &>/dev/null || fail "Homebrew not found. Install from https://brew.sh"
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
    warn "Created config.txt — fill in your tokens after installing"
fi

# ---- ИКОНКА ----
log "Generating icon..."
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
log "Icon created: icon.icns"

# ---- PYINSTALLER ----
log "Building .app with PyInstaller..."
rm -rf build dist

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
    --collect-all "webview" \
    --collect-all "mutagen" \
    --collect-all "lyricsgenius" \
    --collect-all "soundfile" \
    --copy-metadata "mutagen" \
    --copy-metadata "requests" \
    --clean \
    -y \
    "$SCRIPT"

log ".app built: dist/${APP_NAME}.app"

# ---- ПОДПИСЬ ----
log "Signing app (ad-hoc)..."
codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null && \
    log "Signed OK" || warn "Signing failed — app may show Gatekeeper warning"
xattr -cr "dist/${APP_NAME}.app" 2>/dev/null || true

# ---- DMG ----
log "Building DMG..."
rm -f "$DMG_NAME"
DMG_SRC=$(mktemp -d)
cp -r "dist/${APP_NAME}.app" "$DMG_SRC/"

create-dmg \
    --volname "$APP_NAME Installer" \
    --volicon "icon.icns" \
    --window-pos 200 120 \
    --window-size 560 380 \
    --icon-size 100 \
    --icon "${APP_NAME}.app" 140 185 \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link 420 185 \
    --no-internet-enable \
    --skip-jenkins \
    "$DMG_NAME" \
    "$DMG_SRC"

rm -rf "$DMG_SRC"

SIZE=$(du -sh "$DMG_NAME" | cut -f1)
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  Build complete!                 ║${NC}"
echo -e "${GREEN}║                                      ║${NC}"
echo -e "${GREEN}║  📦 ${DMG_NAME} (${SIZE})            ║${NC}"
echo -e "${GREEN}║                                      ║${NC}"
echo -e "${GREEN}║  open ${DMG_NAME}                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"