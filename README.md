# Audio Tagger

A macOS desktop app for music library management with a Liquid Glass-inspired UI.

Built with Python + PyWebView. No subscription required — uses free APIs.

---

## Features

### Tagger
- **Lyrics** — fetches and embeds lyrics via [Genius API](https://genius.com/api-clients). Retries tracks with parentheses in title (e.g. "Song (Album Version)" → "Song")
- **Genre** — fetches genre tags via [Last.fm API](https://www.last.fm/api). Falls back to Genius primary tag
- **ReplayGain** — runs `rsgain easy` on the selected folder (per-track mode)
- **Dynamic Range** — calculates DR14 per the [Pleasurize Music Foundation](https://www.pleasurizemusic.com) standard
- **Year fix** — strips month/day from date tags (`2007-05-14` → `2007`), handles `-`, `/`, `\`, `.` separators
- Live file table with per-file status, RG, DR, Lyrics, Year, Genre columns

### FLAC Checker
- **Source authenticity** — runs [FLAD](https://github.com/Sg4Dylan/FLAD) model to detect fake lossless (MP3/AAC/Opus upscales)
- **Hi-Res analysis** — FFT spectrum check: True Hi-Res / CD upscale / Likely lossy upscale
- **DR14** per file
- Sortable results table with verdict tooltips
- Detailed log saved to folder (`flac_checker_log.txt`)

---

## Requirements

### macOS
- **macOS 12 Monterey or later** (tested on macOS 14 Sonoma and 15 Sequoia)
- Apple Silicon (M1/M2/M3) — native arm64 build
- Intel Macs — not tested, may require rebuilding from source

### External tools (Homebrew)
```bash
brew install rsgain ffmpeg
```

### Python (for building from source)
- Python 3.13 from [python.org](https://www.python.org/downloads/) — required for tkinter support
- Homebrew Python does **not** include tkinter and cannot be used for building

---

## Installation

### Option A — Download DMG (recommended)
1. Download `AudioTagger.dmg` from [Releases](https://github.com/Nik-Grish/Audio-Tagger/releases)
2. Open the DMG and drag `AudioTagger.app` to Applications
3. First launch: right-click → Open (Gatekeeper warning — app is not signed with Apple Developer certificate)

### Option B — Build from source
```bash
# 1. Clone
git clone https://github.com/Nik-Grish/Audio-Tagger.git
cd Audio-Tagger

# 2. Create venv with python.org Python 3.13
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv ~/lyrics-env
source ~/lyrics-env/bin/activate

# 3. Install dependencies
pip install mutagen lyricsgenius tqdm requests numpy soundfile customtkinter pillow pyinstaller pywebview

# 4. Build
chmod +x build.sh
./build.sh
```

---

## Setup

On first launch, go to **Settings** tab and fill in:

| Field | Where to get |
|-------|-------------|
| Genius token | [genius.com/api-clients](https://genius.com/api-clients) — free account |
| Last.fm key | [last.fm/api/account/create](https://www.last.fm/api/account/create) — free account |
| FLAD path | Clone [Sg4Dylan/FLAD](https://github.com/Sg4Dylan/FLAD) — required for FLAC Checker source analysis |
| Log path | Optional — defaults to music folder |

Settings are saved to `config.txt` next to the app.

---

## FLAD Setup (optional)

FLAC Checker source analysis requires FLAD. Without it, only Hi-Res spectrum check and DR14 run.

```bash
git clone https://github.com/Sg4Dylan/FLAD.git ~/FLAD
# Download model from FLAD releases and place in ~/FLAD/models/flad.onnx
```

Then set **FLAD path** to `~/FLAD` in Settings.

---

## Supported formats

| Format | Lyrics | Genre | RG | DR | Year |
|--------|--------|-------|----|----|------|
| FLAC   | ✅ | ✅ | ✅ | ✅ | ✅ |
| MP3    | ✅ | ✅ | ✅ | ✅ | ✅ |
| M4A    | ✅ | ✅ | — | ✅ | ✅ |
| OGG    | ✅ | ✅ | ✅ | ✅ | ✅ |
| WAV    | ✅ | ✅ | — | ✅ | ✅ |
| AIFF   | ✅ | ✅ | — | ✅ | ✅ |

FLAC Checker supports FLAC only.

---

## Known limitations

- **Gatekeeper** — app is signed ad-hoc (not via Apple Developer Program). First launch requires right-click → Open
- **FLAD accuracy** — source detection works best on tracks with strong lossy artifacts. Subtle upscales may be missed
- **Hi-Res detection** — electronically produced music (synths, EDM) may show low high-frequency content regardless of source quality
- **Genre** — Last.fm tags are crowd-sourced and may be imprecise for niche genres

---

## License

MIT

---

## Acknowledgements

- [FLAD](https://github.com/Sg4Dylan/FLAD) — FLAC Lossless Authenticity Detection model by Sg4Dylan
- [lyricsgenius](https://github.com/johnwmillr/LyricsGenius) — Genius API Python client
- [rsgain](https://github.com/complexlogic/rsgain) — ReplayGain utility
- [pywebview](https://pywebview.flowrl.com) — Python + HTML/CSS/JS desktop apps
