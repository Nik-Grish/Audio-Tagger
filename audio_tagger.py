import os
import sys
import re
import time
import threading
import subprocess
import importlib.util
from datetime import datetime

# Dependencies are bundled by PyInstaller when running as .app

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

from mutagen.id3 import ID3, USLT, TDRC, TCON, TXXX
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
import lyricsgenius
import requests
import numpy as np
import soundfile as sf

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

# App version — read from version.txt bundled with the app
def get_app_version() -> str:
    """Read version from version.txt — checks multiple locations."""
    candidates = []
    # 1. PyInstaller bundle
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, 'version.txt'))
    # 2. Next to the script file
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.txt'))
    # 3. Current working directory
    candidates.append(os.path.join(os.getcwd(), 'version.txt'))

    for vpath in candidates:
        try:
            with open(vpath) as f:
                v = f.read().strip()
                if v: return v
        except Exception:
            continue
    return 'v1.0'

APP_VERSION = get_app_version()

# Store config in ~/Library/Application Support/AudioTagger/
# This works both when running as .app and from source
_APP_SUPPORT = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'AudioTagger')
os.makedirs(_APP_SUPPORT, exist_ok=True)
CONFIG_PATH = os.path.join(_APP_SUPPORT, 'config.txt')
SUPPORTED_EXTS = {"mp3", "flac", "m4a", "ogg", "wav", "aiff"}
LASTFM_BASE = 'https://ws.audioscrobbler.com/2.0/'
# Tags that are NOT genres — skip unconditionally
# These are moods, activities, nationalities, or personal tags
_NON_GENRE_TAGS = {
    # Personal/activity tags
    'seen live', 'favorite', 'favourite', 'love', 'owned', 'albums i own',
    # Meaningless quality tags
    'awesome', 'cool', 'great', 'beautiful', 'amazing', 'best',
    # Listener count tags
    'under 2000 listeners', 'under 500 listeners',
    # Nationality/language tags (not genres)
    'canadian', 'american', 'british', 'australian', 'swedish', 'norwegian',
    'finnish', 'german', 'french', 'japanese', 'danish', 'dutch',
    'english', 'irish', 'scottish',
    # Miscellaneous non-genre
    'all', 'male vocalist', 'female vocalist', 'instrumental',
    'straight edge', 'christian', 'atheist',
}

# ============================================================
# CONFIG
# ============================================================

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    cfg[k.strip()] = v.strip().strip('"')
    return cfg

def save_config(cfg: dict):
    lines = []
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            lines = f.readlines()
    
    keys_written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and '=' in stripped and not stripped.startswith('#'):
            k = stripped.split('=', 1)[0].strip()
            if k in cfg:
                new_lines.append(f"{k}={cfg[k]}\n")
                keys_written.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    
    for k, v in cfg.items():
        if k not in keys_written:
            new_lines.append(f"{k}={v}\n")
    
    with open(CONFIG_PATH, 'w') as f:
        f.writelines(new_lines)

# ============================================================
# AUDIO HELPERS (same as CLI version)
# ============================================================

def scan_audio_files(music_root):
    files = []
    for root, _, filenames in os.walk(music_root):
        for name in filenames:
            if name.lower().rsplit('.', 1)[-1] in SUPPORTED_EXTS:
                files.append(os.path.join(root, name))
    return files

def get_tags(filepath):
    ext = filepath.lower().rsplit('.', 1)[-1]
    try:
        if ext == 'mp3':
            t = EasyID3(filepath)
            return t.get('title', [None])[0], t.get('artist', [None])[0]
        elif ext == 'flac':
            t = FLAC(filepath)
            return t.get('title', [None])[0], t.get('artist', [None])[0]
        elif ext == 'm4a':
            t = MP4(filepath); tags = t.tags or {}
            return tags.get('\xa9nam', [None])[0], tags.get('\xa9ART', [None])[0]
        elif ext == 'ogg':
            t = OggVorbis(filepath)
            return t.get('title', [None])[0], t.get('artist', [None])[0]
        elif ext in ('wav', 'aiff'):
            cls = WAVE if ext == 'wav' else AIFF
            t = cls(filepath); tags = t.tags or {}
            return (str(tags['TIT2']) if 'TIT2' in tags else None,
                    str(tags['TPE1']) if 'TPE1' in tags else None)
    except Exception:
        return None, None

def has_lyrics(filepath):
    ext = filepath.lower().rsplit('.', 1)[-1]
    try:
        if ext == 'mp3':
            a = MP3(filepath, ID3=ID3)
            return a.tags is not None and any(
                isinstance(t, USLT) and t.text.strip() for t in a.tags.values())
        elif ext == 'flac':
            a = FLAC(filepath)
            return 'LYRICS' in a and bool(a['LYRICS'][0].strip())
        elif ext == 'm4a':
            return bool((MP4(filepath).tags or {}).get('\xa9lyr'))
        elif ext == 'ogg':
            return bool(OggVorbis(filepath).get('LYRICS'))
        elif ext in ('wav', 'aiff'):
            cls = WAVE if ext == 'wav' else AIFF
            a = cls(filepath)
            return a.tags is not None and any(
                isinstance(t, USLT) and t.text.strip() for t in a.tags.values())
    except Exception:
        return False

def write_lyrics(filepath, lyrics):
    ext = filepath.lower().rsplit('.', 1)[-1]
    if ext == 'mp3':
        a = MP3(filepath, ID3=ID3)
        if a.tags is None: a.tags = ID3()
        a.tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics)); a.save()
    elif ext == 'flac':
        a = FLAC(filepath); a['LYRICS'] = lyrics; a.save()
    elif ext == 'm4a':
        a = MP4(filepath)
        if a.tags is None: a.add_tags()
        a.tags['\xa9lyr'] = [lyrics]; a.save()
    elif ext == 'ogg':
        a = OggVorbis(filepath); a['LYRICS'] = lyrics; a.save()
    elif ext in ('wav', 'aiff'):
        cls = WAVE if ext == 'wav' else AIFF
        a = cls(filepath)
        if a.tags is None: a.add_tags()
        a.tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics)); a.save()

def has_genre(filepath):
    ext = filepath.lower().rsplit('.', 1)[-1]
    try:
        if ext == 'mp3':
            tcon = (MP3(filepath, ID3=ID3).tags or {}).get('TCON')
            return bool(str(tcon.text[0]).strip()) if tcon else False
        elif ext == 'flac':
            return bool(FLAC(filepath).get('genre', [''])[0].strip())
        elif ext == 'm4a':
            return bool((MP4(filepath).tags or {}).get('\xa9gen'))
        elif ext == 'ogg':
            return bool(OggVorbis(filepath).get('genre', [''])[0].strip())
        elif ext in ('wav', 'aiff'):
            cls = WAVE if ext == 'wav' else AIFF
            tcon = (cls(filepath).tags or {}).get('TCON')
            return bool(str(tcon.text[0]).strip()) if tcon else False
    except Exception:
        return False

def write_genre(filepath, genre):
    ext = filepath.lower().rsplit('.', 1)[-1]
    if ext == 'mp3':
        a = MP3(filepath, ID3=ID3)
        if a.tags is None: a.tags = ID3()
        a.tags.add(TCON(encoding=3, text=genre)); a.save()
    elif ext == 'flac':
        a = FLAC(filepath); a['genre'] = genre; a.save()
    elif ext == 'm4a':
        a = MP4(filepath)
        if a.tags is None: a.add_tags()
        a.tags['\xa9gen'] = [genre]; a.save()
    elif ext == 'ogg':
        a = OggVorbis(filepath); a['genre'] = genre; a.save()
    elif ext in ('wav', 'aiff'):
        cls = WAVE if ext == 'wav' else AIFF
        a = cls(filepath)
        if a.tags is None: a.add_tags()
        a.tags.add(TCON(encoding=3, text=genre)); a.save()

def read_year(filepath):
    ext = filepath.lower().rsplit('.', 1)[-1]
    try:
        if ext == 'mp3':
            tdrc = (MP3(filepath, ID3=ID3).tags or {}).get('TDRC')
            return str(tdrc.text[0]) if tdrc else None
        elif ext == 'flac':
            return FLAC(filepath).get('date', [None])[0]
        elif ext == 'm4a':
            v = (MP4(filepath).tags or {}).get('\xa9day'); return v[0] if v else None
        elif ext == 'ogg':
            return OggVorbis(filepath).get('date', [None])[0]
        elif ext in ('wav', 'aiff'):
            cls = WAVE if ext == 'wav' else AIFF
            tdrc = (cls(filepath).tags or {}).get('TDRC')
            return str(tdrc.text[0]) if tdrc else None
    except Exception:
        return None

def write_year(filepath, year):
    ext = filepath.lower().rsplit('.', 1)[-1]
    if ext == 'mp3':
        a = MP3(filepath, ID3=ID3)
        if a.tags is None: a.tags = ID3()
        a.tags.add(TDRC(encoding=3, text=year)); a.save()
    elif ext == 'flac':
        a = FLAC(filepath); a['date'] = year; a.save()
    elif ext == 'm4a':
        a = MP4(filepath)
        if a.tags is None: a.add_tags()
        a.tags['\xa9day'] = [year]; a.save()
    elif ext == 'ogg':
        a = OggVorbis(filepath); a['date'] = year; a.save()
    elif ext in ('wav', 'aiff'):
        cls = WAVE if ext == 'wav' else AIFF
        a = cls(filepath)
        if a.tags is None: a.add_tags()
        a.tags.add(TDRC(encoding=3, text=year)); a.save()

def write_dr_tag(filepath, dr_result):
    ext = filepath.lower().rsplit('.', 1)[-1]
    dr_str = str(dr_result['dr'])
    peak_str = f"{dr_result['peak_db']:.2f} dB"
    if ext == 'mp3':
        a = MP3(filepath, ID3=ID3)
        if a.tags is None: a.tags = ID3()
        a.tags.add(TXXX(encoding=3, desc='DYNAMIC_RANGE', text=dr_str))
        a.tags.add(TXXX(encoding=3, desc='DYNAMIC_RANGE_PEAK', text=peak_str)); a.save()
    elif ext == 'flac':
        a = FLAC(filepath)
        a['DYNAMIC_RANGE'] = dr_str; a['DYNAMIC_RANGE_PEAK'] = peak_str; a.save()
    elif ext == 'm4a':
        a = MP4(filepath)
        if a.tags is None: a.add_tags()
        a.tags['----:com.apple.iTunes:DYNAMIC_RANGE'] = [dr_str.encode()]
        a.tags['----:com.apple.iTunes:DYNAMIC_RANGE_PEAK'] = [peak_str.encode()]; a.save()
    elif ext == 'ogg':
        a = OggVorbis(filepath)
        a['DYNAMIC_RANGE'] = dr_str; a['DYNAMIC_RANGE_PEAK'] = peak_str; a.save()
    elif ext in ('wav', 'aiff'):
        cls = WAVE if ext == 'wav' else AIFF
        a = cls(filepath)
        if a.tags is None: a.add_tags()
        a.tags.add(TXXX(encoding=3, desc='DYNAMIC_RANGE', text=dr_str))
        a.tags.add(TXXX(encoding=3, desc='DYNAMIC_RANGE_PEAK', text=peak_str)); a.save()

def clean_lyrics(raw: str) -> str:
    raw = re.sub(r'^[a-z]{2,3}\|\|', '', raw)
    if re.search(r'\d+\s*Contributors', raw):
        m = re.search(r'\[', raw)
        if m:
            raw = raw[m.start():]
    if 'Read More' in raw:
        raw = re.sub(r'^.*?Read More\s*', '', raw, flags=re.DOTALL)
    raw = re.sub(r'\d*Embed$', '', raw).strip()
    return raw

def fix_year(s: str) -> str:
    """Extract 4-digit year from any date string with any separator."""
    if not s: return s
    # Normalize separators: -, /, \, .  -> space, then find first 4-digit group
    normalized = re.sub(r'[-/\\.]', ' ', s.strip())
    m = re.search(r'\b(\d{4})\b', normalized)
    return m.group(1) if m else s

def _pick_genre(tags: list) -> str | None:
    """
    Pick best genre tag from Last.fm tags list.

    Strategy: Last.fm already sorts tags by count (most votes first).
    We just skip non-genre tags and return the first real genre.
    The most-voted tag is the most representative genre.
    """
    for tag in tags:
        name = tag.get('name', '').strip()
        count = int(tag.get('count', 0))
        if count < 5: continue
        if not name or len(name) < 2 or len(name) > 60: continue
        if name.lower() in _NON_GENRE_TAGS: continue
        return name.title()
    return None

def get_genre_lastfm(artist: str, title: str, lastfm_key: str):
    if not lastfm_key: return None
    try:
        r = requests.get(LASTFM_BASE, params={
            'method': 'track.getTopTags', 'artist': artist, 'track': title,
            'api_key': lastfm_key, 'format': 'json', 'autocorrect': 1,
        }, timeout=8)
        if r.status_code == 200:
            genre = _pick_genre(r.json().get('toptags', {}).get('tag', []))
            if genre: return genre
        time.sleep(0.3)
        r2 = requests.get(LASTFM_BASE, params={
            'method': 'artist.getTopTags', 'artist': artist,
            'api_key': lastfm_key, 'format': 'json', 'autocorrect': 1,
        }, timeout=8)
        if r2.status_code == 200:
            return _pick_genre(r2.json().get('toptags', {}).get('tag', []))
    except Exception:
        pass
    return None

def load_audio_for_dr(filepath):
    ext = filepath.lower().rsplit('.', 1)[-1]
    if ext in ('flac', 'wav', 'ogg', 'mp3', 'aiff'):
        try:
            with sf.SoundFile(filepath) as f:
                return f.read(dtype='float32'), f.samplerate
        except Exception:
            pass
    cmd = [get_ffmpeg_path(), '-i', filepath, '-f', 'f32le', '-ac', '2',
           '-ar', '44100', 'pipe:1', '-loglevel', 'quiet']
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout: return None, None
    return np.frombuffer(result.stdout, dtype='float32').reshape(-1, 2), 44100

def calculate_dr14(filepath):
    data, sr = load_audio_for_dr(filepath)
    if data is None: return None
    channels = [data[:, i] for i in range(data.shape[1])] if data.ndim > 1 else [data]
    block_size = sr * 3
    dr_per_ch = []
    for ch in channels:
        n = len(ch) // block_size
        if n < 2: continue
        blocks = [ch[i*block_size:(i+1)*block_size] for i in range(n)]
        rms = [np.sqrt(np.mean(b**2)) for b in blocks]
        pk = [np.max(np.abs(b)) for b in blocks]
        top = np.argsort(rms)[::-1][:max(1, int(np.ceil(n * 0.2)))]
        tr = np.sqrt(np.mean([rms[i]**2 for i in top]))
        tp = np.max([pk[i] for i in top])
        if tr > 0 and tp > 0:
            dr_per_ch.append(20 * np.log10(tp / tr))
    if not dr_per_ch: return None
    return {
        'dr': int(round(np.mean(dr_per_ch))),
        'peak_db': round(20 * np.log10(max(np.max(np.abs(c)) for c in channels)), 2)
    }

# ============================================================
# FLAC CHECKER LOGIC
# ============================================================

def get_flad_dir() -> str:
    """Returns FLAD directory: bundled inside .app > config.txt > ~/FLAD fallback."""
    # 1. Bundled inside PyInstaller .app
    if getattr(sys, 'frozen', False):
        bundled = os.path.join(sys._MEIPASS, 'flad_bundle')
        if os.path.exists(os.path.join(bundled, 'models', 'flad.onnx')):
            return bundled
    # 2. From config.txt
    cfg_dir = load_config().get('FLAD_DIR', '')
    if cfg_dir and os.path.exists(os.path.join(cfg_dir, 'models', 'flad.onnx')):
        return cfg_dir
    # 3. Default ~/FLAD
    default = os.path.join(os.path.expanduser('~'), 'FLAD')
    if os.path.exists(os.path.join(default, 'models', 'flad.onnx')):
        return default
    return ''

FLAD_DIR = get_flad_dir()

def get_bundled_path(name: str) -> str:
    """Returns path to bundled binary (PyInstaller) or system fallback."""
    if getattr(sys, 'frozen', False):
        # Running inside .app bundle
        bundle_dir = sys._MEIPASS
        bundled = os.path.join(bundle_dir, 'bin', name)
        if os.path.exists(bundled):
            return bundled
    # Fallback to Homebrew
    return f'/opt/homebrew/bin/{name}'

def get_rsgain_path() -> str:
    return get_bundled_path('rsgain')

def get_ffmpeg_path() -> str:
    return get_bundled_path('ffmpeg')

def run_flac_check(filepath, log_lines: list) -> dict:
    """Запускает FLAD + Hi-Res check на одном файле. Возвращает dict с результатами."""
    import sys as _sys
    file = os.path.basename(filepath)
    result = {
        'file': file,
        'source': '—',
        'container': '—',
        'dr': '—',
        'verdict': 'Error',
        'verdict_key': 'error',
    }

    log_lines.append(f"\n{'='*60}")
    log_lines.append(f"File: {file}")
    log_lines.append(f"Path: {filepath}")
    log_lines.append(f"Time: {datetime.now().strftime('%H:%M:%S')}")

    # --- Bit depth & sample rate ---
    try:
        with sf.SoundFile(filepath) as f:
            sr = f.samplerate
            bit_raw = f.subtype_info
            bit_depth = int(''.join(filter(str.isdigit, bit_raw)))
    except Exception as e:
        log_lines.append(f"[ERROR] Could not read audio specs: {e}")
        return result

    sr_khz = sr / 1000
    result['container'] = f"{bit_depth}bit / {sr_khz:.1f}kHz"
    log_lines.append(f"\n[Container] {bit_depth}bit / {sr_khz:.1f}kHz")

    # --- FLAD ---
    source_label = 'Lossless'
    source_prob = 100.0
    pred_format = 'FLAC'
    lossy_ratio = 0.0

    FLAD_DIR = get_flad_dir()
    if FLAD_DIR and os.path.exists(FLAD_DIR):
        try:
            orig_cwd = os.getcwd()
            orig_path = _sys.path[:]

            # Set CWD to FLAD_DIR so relative paths in FLAD code work
            os.chdir(FLAD_DIR)

            # Add both FLAD_DIR and its parent to sys.path
            for p in [FLAD_DIR, os.path.dirname(FLAD_DIR)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)

            # Import FLAD — try package import first, then direct file
            FLADModel = None
            try:
                # Standard package: FLAD_DIR/flad/eval.py
                from flad.eval import FLAD as FLADModel
                from flad import utils
            except ImportError:
                # Bundled flat layout: FLAD_DIR/eval.py + utils.py
                import importlib.util as _ilu
                for fname, modname in [('eval.py','_flad_eval'),('eval1.py','_flad_eval')]:
                    fpath = os.path.join(FLAD_DIR, fname)
                    if os.path.exists(fpath) and FLADModel is None:
                        spec = _ilu.spec_from_file_location(modname, fpath)
                        mod = _ilu.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        FLADModel = mod.FLAD
                for fname, modname in [('utils.py','_flad_utils'),('utils1.py','_flad_utils')]:
                    fpath = os.path.join(FLAD_DIR, 'flad', fname)
                    if not os.path.exists(fpath):
                        fpath = os.path.join(FLAD_DIR, fname)
                    if os.path.exists(fpath):
                        spec = _ilu.spec_from_file_location(modname, fpath)
                        utils = _ilu.module_from_spec(spec)
                        spec.loader.exec_module(utils)
                        break

            if FLADModel is None:
                raise ImportError("Could not load FLAD model class")

            flad = FLADModel()

            import numpy as _np

            # Use temp dir inside FLAD_DIR to avoid CWD issues
            temp_dir = os.path.join(FLAD_DIR, '_temp_spectra')
            os.makedirs(temp_dir, exist_ok=True)

            r_map = ['FLAC', 'AAC', 'mp3', 'Opus']
            y_s = utils.get_side(filepath)
            utils.get_spectrum(y_s, 0, temp_dir, max=20)
            spectrum_list = utils.get_file_list(temp_dir, ext='.jpg')

            counter = _np.zeros(4)
            probs_list = []

            log_lines.append(f"\n[FLAD] Analyzing {len(spectrum_list)} spectrum samples...")
            for idx, img_path in enumerate(spectrum_list):
                norm_img = flad._img_preprocess(img_path)
                if norm_img is None: continue
                output = flad.session.run([], {flad.model_input: norm_img})[0][0]
                e_x = _np.exp(output - _np.max(output))
                probs = e_x / e_x.sum()
                pred = _np.argmax(probs)
                counter[pred] += 1
                probs_list.append((r_map[pred], float(probs[pred])))
                log_lines.append(f"  Sample {idx+1:2d}: {r_map[pred]} ({probs[pred]*100:.1f}%)")

            total = len(probs_list)
            lossy_count = sum(1 for fmt, _ in probs_list if fmt != 'FLAC')
            lossy_ratio = lossy_count / total if total > 0 else 0

            log_lines.append(f"\n[FLAD] Lossy samples: {lossy_count}/{total} ({lossy_ratio*100:.1f}%)")

            if lossy_ratio > 0.10:
                lossy_fmts = [fmt for fmt, _ in probs_list if fmt != 'FLAC']
                pred_format = max(set(lossy_fmts), key=lossy_fmts.count)
                source_prob = _np.mean([p for fmt, p in probs_list if fmt == pred_format]) * 100
                source_label = f'{pred_format} ({source_prob:.0f}%)'
                log_lines.append(f"[FLAD] Result: FAKE — source is {pred_format} ({source_prob:.1f}%)")
            else:
                flac_probs = [p for fmt, p in probs_list if fmt == 'FLAC']
                source_prob = _np.mean(flac_probs) * 100 if flac_probs else 100.0
                source_label = 'Lossless'
                log_lines.append(f"[FLAD] Result: Lossless ({source_prob:.1f}%)")

            # Clean up temp spectra
            import shutil as _shutil
            try: _shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception: pass
            os.chdir(orig_cwd)
            _sys.path = orig_path
        except Exception as e:
            log_lines.append(f"[FLAD] Error: {e} — skipping FLAD analysis")
    else:
        log_lines.append("[FLAD] FLAD not found — skipping source check")

    result['source'] = source_label

    # --- Hi-Res check ---
    hires_verdict = 'cd_container'
    if sr > 44100 and bit_depth > 16:
        try:
            with sf.SoundFile(filepath) as f:
                total_frames = f.frames
            duration_min = total_frames / sr / 60
            chunk = sr * 20
            starts = ([0, total_frames // 2] if duration_min <= 5
                      else [total_frames // 3 - chunk // 2, total_frames * 2 // 3 - chunk // 2])
            starts = [max(0, s) for s in starts]

            ratios = []
            for start in starts:
                with sf.SoundFile(filepath) as f:
                    f.seek(start)
                    data = f.read(min(chunk, total_frames - start), dtype='float32')
                if data.ndim > 1: data = data.mean(axis=1)
                fft = np.abs(np.fft.rfft(data))
                freqs = np.fft.rfftfreq(len(data), 1 / sr)
                def band(lo, hi): 
                    mask = (freqs >= lo) & (freqs < hi)
                    return float(np.mean(fft[mask])) if mask.any() else 0.0
                e_base = band(1000, 16000)
                if e_base > 0:
                    ratios.append((
                        band(16000, 22050) / e_base,
                        band(22050, 28050) / e_base,
                        band(28050, sr / 2) / e_base,
                    ))

            if ratios:
                r_mid = max(r[0] for r in ratios)
                r_ulo = max(r[1] for r in ratios)
                r_ult = max(r[2] for r in ratios)
                log_lines.append(f"\n[Hi-Res] 16-22kHz: {r_mid*100:.2f}% | 22-28kHz: {r_ulo*100:.2f}% | 28kHz+: {r_ult*100:.2f}%")
                if r_ult > 0.003 or r_ulo > 0.008:
                    hires_verdict = 'true_hires'
                    log_lines.append("[Hi-Res] Verdict: True Hi-Res — content above 22kHz confirmed")
                elif r_mid > 0.05:
                    hires_verdict = 'possible_cd_upscale'
                    log_lines.append("[Hi-Res] Verdict: Possible CD upscale — limited content above 22kHz")
                else:
                    hires_verdict = 'likely_lossy_upscale'
                    log_lines.append("[Hi-Res] Verdict: Likely lossy upscale — very little content above 16kHz")
        except Exception as e:
            log_lines.append(f"[Hi-Res] Error: {e}")

    # --- DR ---
    dr_result = calculate_dr14(filepath)
    if dr_result:
        result['dr'] = f"DR{dr_result['dr']}"
        log_lines.append(f"\n[DR14] DR{dr_result['dr']} | Peak: {dr_result['peak_db']:.2f} dB")
    else:
        log_lines.append("\n[DR14] Could not calculate")

    # --- Final verdict ---
    if pred_format != 'FLAC':
        verdict = 'Fake lossless'
        verdict_key = 'fake'
        log_lines.append(f"\n[Verdict] FAKE LOSSLESS — {pred_format} source detected")
    elif hires_verdict == 'cd_container':
        verdict = 'Proper CD'
        verdict_key = 'cd'
        log_lines.append(f"\n[Verdict] Proper CD — 16bit/44.1kHz")
    elif hires_verdict == 'true_hires':
        verdict = 'Genuine Hi-Res'
        verdict_key = 'hires'
        log_lines.append(f"\n[Verdict] GENUINE HI-RES — lossless source, real content above 22kHz")
    elif hires_verdict == 'possible_cd_upscale':
        verdict = 'CD upscale'
        verdict_key = 'upscale'
        log_lines.append(f"\n[Verdict] CD UPSCALE — lossless source but limited high-frequency content")
    else:
        verdict = 'Low HF content'
        verdict_key = 'suspicious'
        log_lines.append(f"\n[Verdict] LOW HF CONTENT — very little energy above 16kHz, possible lossy upscale or heavily processed master")

    result['verdict'] = verdict
    result['verdict_key'] = verdict_key
    return result

# ============================================================
# MAIN APP
# ============================================================

import json
import webview

# ============================================================
# HTML/CSS/JS FRONTEND
# ============================================================



HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Audio Tagger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --glass-bg:rgba(255,255,255,0.08);--glass-border:rgba(255,255,255,0.18);
  --glass-input:rgba(255,255,255,0.07);--text:rgba(255,255,255,0.92);
  --text-muted:rgba(255,255,255,0.45);--sep:rgba(255,255,255,0.1);
  --sidebar-w:176px;--radius:14px;
  --font:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
  --mono:'Menlo','SF Mono',monospace;
}
body.light{
  --glass-bg:rgba(255,255,255,0.52);--glass-border:rgba(255,255,255,0.85);
  --glass-input:rgba(255,255,255,0.65);--text:rgba(0,0,0,0.88);
  --text-muted:rgba(0,0,0,0.45);--sep:rgba(0,0,0,0.1);
}
html,body{height:100%;overflow:hidden}
body{font-family:var(--font);background:#060614;color:var(--text);display:flex;flex-direction:column;height:100vh;transition:color .3s}
body.light{background:#dce8fa}

.scene{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.orb{position:absolute;border-radius:50%;filter:blur(72px);opacity:.38}
body.light .orb{opacity:.2}
.o1{width:420px;height:420px;background:#3b82f6;top:-120px;left:-90px}
.o2{width:360px;height:360px;background:#8b5cf6;top:40px;right:-70px}
.o3{width:320px;height:320px;background:#06b6d4;bottom:-60px;left:32%}

.titlebar{height:44px;display:flex;align-items:center;padding:0 14px;gap:8px;flex-shrink:0;position:relative;z-index:10}
.drag-area{position:absolute;top:0;left:0;width:120px;height:44px;-webkit-app-region:drag;-webkit-user-select:none}
.tb-icon{font-size:18px;margin-left:86px;opacity:.7}
.win-title{font-size:13px;font-weight:500;color:var(--text-muted);margin-left:4px}
.theme-toggle{margin-left:auto;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:20px;padding:4px 12px;font-size:12px;color:var(--text-muted);cursor:pointer;font-family:var(--font);transition:all .2s}
.theme-toggle:hover{color:var(--text);background:rgba(255,255,255,0.12)}

.layout{display:flex;flex:1;overflow:hidden;position:relative;z-index:2;padding:0 12px 12px;gap:10px}

.sidebar{width:var(--sidebar-w);flex-shrink:0;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:var(--radius);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);padding:10px 7px;display:flex;flex-direction:column;gap:3px;position:relative;overflow:hidden}
.sidebar::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4),transparent)}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 11px;border-radius:10px;font-size:13px;color:var(--text-muted);cursor:pointer;transition:all .15s;user-select:none}
.nav-item:hover{background:rgba(255,255,255,0.08);color:var(--text)}
.nav-item.active{background:rgba(255,255,255,0.13);border:1px solid rgba(255,255,255,0.2);color:var(--text);box-shadow:0 2px 12px rgba(0,0,0,0.2)}
.nav-icon{font-size:15px;width:18px;text-align:center;flex-shrink:0}
.sidebar-sep{height:1px;background:var(--sep);margin:5px 3px}
.sidebar-footer{margin-top:auto;padding:8px 4px 2px;border-top:1px solid var(--sep)}
.sidebar-version{font-size:10px;color:var(--text-muted);padding:3px 8px;opacity:.7}
.sidebar-link{display:block;font-size:10px;color:var(--text-muted);padding:3px 8px;text-decoration:none;opacity:.7;transition:opacity .15s}
.sidebar-link:hover{opacity:1;color:var(--text)}

.content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.panel{display:none;flex-direction:column;height:100%;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:var(--radius);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);padding:14px;overflow:hidden;position:relative}
.panel::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.32),transparent)}
.panel.active{display:flex}

.field-row{display:flex;align-items:center;gap:7px;margin-bottom:9px}
.field-lbl{font-size:12px;color:var(--text-muted);width:86px;flex-shrink:0}
.glass-input{flex:1;background:var(--glass-input);border:1px solid var(--glass-border);border-radius:10px;padding:7px 10px;font-size:12px;color:var(--text);font-family:var(--mono);outline:none;transition:border .2s}
.glass-input:focus{border-color:rgba(10,132,255,.6)}
.glass-btn{background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:9px;padding:7px 12px;font-size:12px;color:var(--text-muted);cursor:pointer;transition:all .15s;white-space:nowrap;font-family:var(--font)}
.glass-btn:hover{background:rgba(255,255,255,0.13);color:var(--text)}
.glass-btn.icon-btn{padding:7px 10px;font-size:14px}
.sep{height:1px;background:var(--sep);margin:9px 0}

.opts-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px}
.opt{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;position:relative}
.glass-check{width:16px;height:16px;border-radius:5px;border:1px solid rgba(255,255,255,0.3);background:rgba(10,132,255,.55);display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .15s;position:relative;overflow:hidden}
.glass-check::after{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:rgba(255,255,255,0.18)}
.glass-check.off{background:rgba(255,255,255,0.07)}
.glass-check svg{position:relative;z-index:1}
.opt-lbl{font-size:13px;color:var(--text)}
.opt-lbl.dim{color:var(--text-muted)}

.tip{display:none;position:absolute;left:0;top:calc(100% + 6px);z-index:100;background:rgba(20,20,40,0.96);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:8px 11px;font-size:11px;line-height:1.5;color:rgba(255,255,255,0.85);white-space:normal;width:220px;pointer-events:none;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,0.4)}
body.light .tip{background:rgba(240,245,255,0.97);color:rgba(0,0,0,0.8);border-color:rgba(0,0,0,.12)}
.opt:hover .tip{display:block}

.btn-row{display:flex;gap:7px;margin-bottom:9px}
.btn-run{flex:1;background:rgba(10,132,255,.55);border:1px solid rgba(10,132,255,.7);border-radius:12px;padding:8px;font-size:14px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.btn-run::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent)}
.btn-run:hover{filter:brightness(1.15)}.btn-run:disabled{opacity:.45;cursor:not-allowed}
.btn-stop{background:rgba(255,69,58,.55);border:1px solid rgba(255,69,58,.65);border-radius:12px;padding:8px 18px;font-size:14px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.btn-stop::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.15),transparent)}
.btn-stop:hover{filter:brightness(1.15)}.btn-stop:disabled{opacity:.3;cursor:not-allowed}

.progress-wrap{height:3px;background:rgba(255,255,255,0.09);border-radius:2px;margin-bottom:8px;overflow:hidden;flex-shrink:0}
.progress-fill{height:100%;width:0%;background:linear-gradient(90deg,#0a84ff,#30d158);border-radius:2px;transition:width .3s ease}

/* Tagger table */
.tbl-wrap{flex:1;overflow:auto;border-radius:10px;border:1px solid var(--sep);margin-bottom:0}
.tbl-wrap::-webkit-scrollbar{width:6px}.tbl-wrap::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:3px}
table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}
thead{position:sticky;top:0;z-index:1}
th{text-align:left;font-size:11px;font-weight:500;color:var(--text-muted);padding:7px 9px;background:rgba(255,255,255,0.06);border-bottom:1px solid var(--sep);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--text)}
td{padding:7px 9px;border-bottom:1px solid rgba(255,255,255,0.05);color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:text;user-select:text;-webkit-user-select:text}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,0.04)}
body.light th{background:rgba(0,0,0,0.04)}
body.light td{border-bottom-color:rgba(0,0,0,0.06)}
body.light tr:hover td{background:rgba(0,0,0,0.03)}
.finder-btn{background:none;border:none;cursor:pointer;font-size:13px;opacity:.5;padding:0 2px;transition:opacity .15s;-webkit-user-select:none;user-select:none}
.finder-btn:hover{opacity:1}
/* Column resizer */
th{position:relative}
.col-resizer{position:absolute;right:0;top:0;bottom:0;width:5px;cursor:col-resize;user-select:none;-webkit-user-select:none;z-index:2}
.col-resizer:hover,.col-resizer.active{background:rgba(255,255,255,0.25)}
#taggerTable{table-layout:fixed}

.statusbar{flex-shrink:0;margin-top:7px;display:flex;align-items:center;gap:8px;min-height:22px}
.status-text{font-size:11px;color:var(--text-muted);cursor:pointer;transition:color .15s;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status-text:hover{color:var(--text);text-decoration:underline}
.status-text.err{color:#ff6b6b}

/* Log popup */
.log-overlay{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,0.5);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);align-items:center;justify-content:center}
.log-overlay.open{display:flex}
.log-popup{background:rgba(18,18,35,0.97);border:1px solid rgba(255,255,255,0.15);border-radius:16px;width:680px;max-height:70vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,0.6)}
body.light .log-popup{background:rgba(240,245,255,0.98);border-color:rgba(0,0,0,.12)}
.log-popup-header{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--sep)}
.log-popup-title{font-size:13px;font-weight:500;flex:1}
.log-popup-close{background:none;border:none;color:var(--text-muted);font-size:18px;cursor:pointer;padding:0 4px;line-height:1}
.log-popup-close:hover{color:var(--text)}
.log-content{flex:1;overflow-y:auto;padding:12px 14px;font-family:var(--mono);font-size:11px;line-height:1.7;user-select:text;-webkit-user-select:text}
.log-content::-webkit-scrollbar{width:6px}.log-content::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:3px}
.log-ok{color:#30d158}.log-err{color:#ff453a}.log-info{color:#64d2ff}.log-dim{color:var(--text-muted)}
.log-line{margin:0}

/* Checker */
.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;position:relative;cursor:default}
.b-ok{background:rgba(48,209,88,.15);border:1px solid rgba(48,209,88,.4);color:#30d158}
.b-warn{background:rgba(255,214,10,.18);border:1px solid rgba(255,180,0,.55);color:#b8860b}
body.light .b-warn{background:rgba(180,120,0,.18);border-color:rgba(150,100,0,.5);color:#7a5800}
.b-err{background:rgba(255,69,58,.15);border:1px solid rgba(255,69,58,.4);color:#ff453a}
.b-cd{background:rgba(0,100,200,.18);border:1px solid rgba(0,120,220,.5);color:#1a6fbf}
body.light .b-cd{background:rgba(0,80,180,.15);border-color:rgba(0,80,160,.5);color:#0040a0}
.b-susp{background:rgba(255,159,10,.15);border:1px solid rgba(255,159,10,.4);color:#cc7700}
body.light .b-susp{background:rgba(180,100,0,.15);border-color:rgba(150,80,0,.4);color:#7a4400}

#floatTip{display:none;position:fixed;z-index:999;background:rgba(20,20,40,0.97);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:9px 12px;font-size:11px;line-height:1.6;color:rgba(255,255,255,0.88);max-width:280px;pointer-events:none;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,.5)}
body.light #floatTip{background:rgba(240,245,255,0.98);color:rgba(0,0,0,0.82);border-color:rgba(0,0,0,.12)}

.checker-progress-wrap{height:3px;background:rgba(255,255,255,0.08);border-radius:2px;margin-top:8px;overflow:hidden;flex-shrink:0}
.checker-progress-fill{height:100%;width:0%;background:linear-gradient(90deg,#0a84ff,#30d158);transition:width .3s ease}
.summary{font-size:11px;color:var(--text-muted);padding:6px 0 0;flex-shrink:0}
.summary b{color:var(--text);font-weight:500}

/* FLAD banner */
.flad-banner{background:rgba(255,159,10,0.12);border:1px solid rgba(255,159,10,0.3);border-radius:10px;padding:10px 14px;margin-bottom:10px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.flad-banner.hidden{display:none}
.flad-banner-text{flex:1;font-size:12px;color:rgba(255,159,10,0.9)}
body.light .flad-banner-text{color:#7a4400}
.flad-install-btn{background:rgba(255,159,10,0.3);border:1px solid rgba(255,159,10,0.5);border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;color:white;cursor:pointer;font-family:var(--font);white-space:nowrap}
.flad-install-btn:hover{filter:brightness(1.2)}

/* Settings */
.save-btn{display:inline-block;background:rgba(10,132,255,.55);border:1px solid rgba(10,132,255,.7);border-radius:10px;padding:8px 20px;font-size:13px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.save-btn::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent)}
.save-btn:hover{filter:brightness(1.15)}
.settings-note{font-size:12px;color:var(--text-muted);line-height:1.6;background:var(--glass-input);border:1px solid var(--sep);border-radius:10px;padding:10px 12px;margin-top:10px}
.settings-note a{color:#64d2ff}
body.light .settings-note a{color:#0066cc}
.settings-scroll{flex:1;overflow-y:auto;padding-right:4px}
.settings-scroll::-webkit-scrollbar{width:4px}.settings-scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:2px}
</style>
</head>
<body id="body">
<div class="scene"><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>

<!-- LOG POPUP -->
<div class="log-overlay" id="logOverlay" onclick="closeLogIfOutside(event)">
  <div class="log-popup">
    <div class="log-popup-header">
      <span class="log-popup-title">Operation Log</span>
      <button class="log-popup-close" onclick="closeLog()">✕</button>
    </div>
    <div class="log-content" id="logContent"></div>
  </div>
</div>

<!-- FLOAT TIP -->
<div id="floatTip"></div>

<div class="titlebar">
  <div class="drag-area"></div>
  <span class="tb-icon">🎧</span>
  <span class="win-title">Audio Tagger</span>
  <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn">🌙 Dark</button>
</div>

<div class="layout">
  <nav class="sidebar">
    <div class="nav-item active" onclick="showPanel('tagger',this)"><span class="nav-icon">🎵</span> Tagger</div>
    <div class="nav-item" onclick="showPanel('checker',this)"><span class="nav-icon">🔍</span> FLAC Checker</div>
    <div class="sidebar-sep"></div>
    <div class="nav-item" onclick="showPanel('settings',this)"><span class="nav-icon">⚙️</span> Settings</div>
    <div class="sidebar-footer">
      <div class="sidebar-version">v 1.0</div>
      <a class="sidebar-link" href="https://github.com/Nik-Grish/Audio-Tagger">⌥ GitHub</a>
    </div>
  </nav>

  <div class="content">

    <!-- TAGGER -->
    <div class="panel active" id="panel-tagger">
      <div class="field-row">
        <span class="field-lbl">Music folder</span>
        <input class="glass-input" id="music-path" placeholder="Select a folder…">
        <button class="glass-btn icon-btn" onclick="browseFolder('music-path')" title="Browse">📁</button>
        <button class="glass-btn icon-btn" onclick="rescanFolder()" title="Rescan folder">🔄</button>
      </div>
      <div class="sep"></div>
      <div class="opts-grid">
        <div class="opt" onclick="toggleCb('cb-lyrics')">
          <div class="glass-check" id="cb-lyrics"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Add lyrics</span>
          <div class="tip">Searches Genius API.<br><b>Skips</b> if lyrics tag already present.<br>Tries full title first, then without parentheses.<br>Enable "Overwrite all" to force-replace.</div>
        </div>
        <div class="opt" onclick="toggleCb('cb-genre')">
          <div class="glass-check" id="cb-genre"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Fetch genre</span>
          <div class="tip">Fetches top tag from Last.fm (track → artist fallback).<br><b>Skips</b> if genre tag already present.</div>
        </div>
        <div class="opt" onclick="toggleCb('cb-rg')">
          <div class="glass-check" id="cb-rg"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">ReplayGain</span>
          <div class="tip">Runs rsgain on the entire folder.<br><b>Always overwrites</b> existing RG tags.</div>
        </div>
        <div class="opt" onclick="toggleCb('cb-dr')">
          <div class="glass-check" id="cb-dr"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Dynamic Range</span>
          <div class="tip">Calculates DR14 (Pleasurize Music Foundation standard).<br><b>Always overwrites</b> existing DR tag.</div>
        </div>
        <div class="opt" onclick="toggleCb('cb-year')">
          <div class="glass-check" id="cb-year"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Fix year tags</span>
          <div class="tip">Strips month/day: 2007-05-14 → 2007<br><b>Skips</b> if already 4-digit year.</div>
        </div>
        <div class="opt" onclick="toggleCb('cb-overwrite')">
          <div class="glass-check off" id="cb-overwrite"></div>
          <span class="opt-lbl dim">Overwrite all</span>
          <div class="tip">Forces re-fetch of lyrics and genre even if tags are already present.</div>
        </div>
      </div>
      <div class="sep"></div>
      <div class="btn-row">
        <button class="btn-run" id="runBtn" onclick="runTagger()">▶ Run</button>
        <button class="btn-stop" id="stopBtn" onclick="stopTagger()" disabled>⏹ Stop</button>
      </div>
      <div class="progress-wrap"><div class="progress-fill" id="taggerProgress"></div></div>
      <div class="tbl-wrap" id="taggerTblWrap">
        <table id="taggerTable">
          <thead><tr>
            <th style="width:28px;text-align:center"> </th>
            <th style="width:24%" onclick="sortTagger(1)">File ↕<div class="col-resizer" onmousedown="startResize(event,this)"></div></th>
            <th style="width:36px;text-align:center">📁</th>
            <th style="width:9%;text-align:center" onclick="sortTagger(3)">RG ↕<div class="col-resizer" onmousedown="startResize(event,this)"></div></th>
            <th style="width:9%;text-align:center" onclick="sortTagger(4)">DR ↕<div class="col-resizer" onmousedown="startResize(event,this)"></div></th>
            <th style="width:9%;text-align:center" onclick="sortTagger(5)">Lyrics ↕<div class="col-resizer" onmousedown="startResize(event,this)"></div></th>
            <th style="width:13%;text-align:center" onclick="sortTagger(6)">Year ↕<div class="col-resizer" onmousedown="startResize(event,this)"></div></th>
            <th onclick="sortTagger(7)">Genre ↕</th>
          </tr></thead>
          <tbody id="taggerBody"></tbody>
        </table>
      </div>
      <div class="statusbar">
        <span class="status-text" id="statusText" onclick="openLogPopup()">Select a folder to begin</span>
      </div>
    </div>

    <!-- FLAC CHECKER -->
    <div class="panel" id="panel-checker">
      <div class="flad-banner hidden" id="fladBanner">
        <span class="flad-banner-text">⚠️ FLAD not installed — source analysis unavailable. Hi-Res spectrum check and DR14 will still run.</span>
        <button class="flad-install-btn" onclick="installFlad('fladBannerStatus')">⬇ Install FLAD</button>
      </div>
      <div id="fladBannerStatus" style="font-size:11px;color:var(--text-muted);margin-bottom:6px;display:none"></div>
      <div class="field-row" style="margin-bottom:12px">
        <input class="glass-input" id="checker-path" placeholder="Select a folder…" oninput="document.getElementById('scanBtn').disabled=!this.value.trim()">
        <button class="glass-btn" onclick="browseFolder('checker-path')">Browse</button>
        <button class="btn-run" style="flex:none;padding:7px 22px;font-size:13px;border-radius:10px" onclick="runChecker()" id="scanBtn" disabled>Scan</button>
        <button class="glass-btn" onclick="openCheckerLog()">Log</button>
      </div>
      <div class="tbl-wrap">
        <table id="checkerTable">
          <thead><tr>
            <th style="width:30%" onclick="sortChecker(0)">File ↕</th>
            <th style="width:14%" onclick="sortChecker(1)">Source ↕</th>
            <th style="width:17%" onclick="sortChecker(2)">Container ↕</th>
            <th style="width:8%;text-align:center" onclick="sortChecker(3)">DR ↕</th>
            <th onclick="sortChecker(4)">Verdict ↕</th>
          </tr></thead>
          <tbody id="checkerBody"></tbody>
        </table>
      </div>
      <div class="summary" id="checkerSummary">Ready to scan</div>
      <div class="checker-progress-wrap"><div class="checker-progress-fill" id="checkerProgress"></div></div>
    </div>

    <!-- SETTINGS -->
    <div class="panel" id="panel-settings">
      <div class="settings-scroll">
        <div class="field-row">
          <span class="field-lbl">Genius token</span>
          <input class="glass-input" id="genius-token" type="password" placeholder="Genius API token">
        </div>
        <div class="field-row">
          <span class="field-lbl">Last.fm key</span>
          <input class="glass-input" id="lastfm-key" type="password" placeholder="Last.fm API key">
        </div>
        <div class="field-row">
          <span class="field-lbl">Music folder</span>
          <input class="glass-input" id="settings-music" placeholder="Default music folder">
          <button class="glass-btn icon-btn" onclick="browseFolder('settings-music')" title="Browse">📁</button>
        </div>
        <div class="field-row">
          <span class="field-lbl">Log folder</span>
          <input class="glass-input" id="log-path" placeholder="Empty = saves to music folder">
          <button class="glass-btn icon-btn" onclick="browseFolder('log-path')" title="Browse">📁</button>
        </div>
        <div class="field-row">
          <span class="field-lbl">FLAD path</span>
          <input class="glass-input" id="flad-dir" placeholder="/path/to/FLAD (optional)">
        </div>
        <div class="sep"></div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="save-btn" onclick="saveSettings()">Save</button>
          <button class="glass-btn" onclick="openConfig()">📄 Open config.txt</button>
        </div>

        <div class="sep"></div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
          <b style="color:var(--text)">App updates</b><br>
          Current version: <b style="color:var(--text)" class="app-version">v1.0</b>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="glass-btn" onclick="checkUpdates()" id="checkUpdBtn">🔄 Check for updates</button>
          <button class="save-btn" id="downloadUpdateBtn" style="display:none" onclick="downloadUpdate()">⬇ Download update</button>
        </div>
        <div id="updateStatus" style="font-size:11px;color:var(--text-muted);margin-top:8px;line-height:1.8"></div>
        <div class="settings-note">
          Genius token: <a href="https://genius.com/api-clients">genius.com/api-clients</a><br>
          Last.fm key: <a href="https://www.last.fm/api/account/create">last.fm/api/account/create</a><br>
          Config: <code>~/Library/Application Support/AudioTagger/config.txt</code>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
// ─── THEME ───────────────────────────────────────────────
let isDark=true;
function toggleTheme(){
  isDark=!isDark;
  document.getElementById('body').className=isDark?'':'light';
  document.getElementById('themeBtn').textContent=isDark?'🌙 Dark':'☀️ Light';
}

// ─── NAV ─────────────────────────────────────────────────
function showPanel(name,el){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  el.classList.add('active');
}

// ─── CHECKBOXES ──────────────────────────────────────────
const cbState={'cb-lyrics':true,'cb-genre':true,'cb-rg':true,'cb-dr':true,'cb-year':true,'cb-overwrite':false};
function toggleCb(id){
  cbState[id]=!cbState[id];
  const el=document.getElementById(id);
  if(cbState[id]){
    el.classList.remove('off');
    el.innerHTML='<svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg>';
    const lbl=el.nextElementSibling;if(lbl)lbl.classList.remove('dim');
  }else{
    el.classList.add('off');el.innerHTML='';
    const lbl=el.nextElementSibling;if(lbl)lbl.classList.add('dim');
  }
}

// ─── BROWSE ──────────────────────────────────────────────
function browseFolder(inputId){
  window.pywebview.api.browse_folder().then(path=>{
    if(!path)return;
    document.getElementById(inputId).value=path;
    if(inputId==='music-path'){
      // Sync checker only if empty
      const cp=document.getElementById('checker-path');
      if(!cp.value.trim())cp.value=path;
      // Save to config immediately
      window.pywebview.api.save_music_root(path);
      loadFileList();
    }
    if(inputId==='checker-path'){
      document.getElementById('scanBtn').disabled=!path.trim();
    }
    if(inputId==='settings-music'){
      document.getElementById('music-path').value=path;
      window.pywebview.api.save_music_root(path);
    }
  });
}

// ─── LOG POPUP ───────────────────────────────────────────
let logLines=[];
function appendLog(msg,type){
  const cls=type==='ok'?'log-ok':type==='err'?'log-err':type==='info'?'log-info':'log-dim';
  logLines.push('<p class="log-line"><span class="'+cls+'">'+escHtml(msg)+'</span></p>');
  document.getElementById('logContent').innerHTML=logLines.join('');
}
function openLogPopup(){
  document.getElementById('logOverlay').classList.add('open');
  const lc=document.getElementById('logContent');
  lc.scrollTop=lc.scrollHeight;
}
function closeLog(){document.getElementById('logOverlay').classList.remove('open')}
function closeLogIfOutside(e){if(e.target===document.getElementById('logOverlay'))closeLog()}

// ─── STATUS ──────────────────────────────────────────────
function setStatus(msg,isErr){
  const el=document.getElementById('statusText');
  el.textContent=msg+(msg&&!isErr?' — click to see log':'');
  el.className='status-text'+(isErr?' err':'');
}

// ─── TAGGER TABLE ────────────────────────────────────────
let taggerRows={};
let taggerSortDir={};

function loadFileList(){
  const path=document.getElementById('music-path').value.trim();
  if(!path){setStatus('Select a folder to begin');return;}
  taggerRows={};
  document.getElementById('taggerBody').innerHTML='';
  setStatus('Scanning folder…');
  window.pywebview.api.get_file_list(path).then(files=>{
    if(!files||!files.length){setStatus('No audio files found');return;}
    files.forEach(f=>addTaggerRow(f,'pending'));
    setStatus('Ready — '+files.length+' files found');
  }).catch(e=>setStatus('Error scanning: '+e,true));
}

function rescanFolder(){loadFileList();}

function addTaggerRow(fi,status){
  const id='r'+Math.random().toString(36).slice(2);
  taggerRows[fi.path]={...fi,status,rg:fi.rg||'—',dr:fi.dr||'—',
    lyrics:fi.lyrics?'Yes':'No',year:fi.year||'—',genre:fi.genre||'—',id};
  const tbody=document.getElementById('taggerBody');
  const tr=document.createElement('tr');tr.id=id;tr.innerHTML=rowHtml(taggerRows[fi.path]);
  tbody.appendChild(tr);
}

function updateTaggerRow(path,updates){
  if(!taggerRows[path])return;
  Object.assign(taggerRows[path],updates);
  const tr=document.getElementById(taggerRows[path].id);
  if(tr)tr.innerHTML=rowHtml(taggerRows[path]);
}

const STATUS_ICON={pending:'○',running:'⏳',ok:'✅',err:'❌',skip:'⏩'};

function rowHtml(r){
  const icon=STATUS_ICON[r.status]||'○';
  const lyricsCls=r.lyrics==='Yes'?'style="color:#30d158"':'style="color:var(--text-muted)"';
  const enc=encodeURIComponent(r.path);
  return `<td style="text-align:center;font-size:14px">${icon}</td>
    <td title="${escHtml(r.path)}">${escHtml(r.file)}</td>
    <td style="text-align:center"><button class="finder-btn" onclick="revealInFinder('${escHtml(r.path)}')" title="Show in Finder">🔍</button></td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.rg)}</td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.dr)}</td>
    <td style="text-align:center" ${lyricsCls}>${escHtml(r.lyrics)}</td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.year)}</td>
    <td>${escHtml(r.genre)}</td>`;
}

function revealInFinder(path){
  window.pywebview.api.reveal_in_finder(path);
}

function sortTagger(col){
  const keys=['status','file','finder','rg','dr','lyrics','year','genre'];
  const key=keys[col];
  if(key==='finder')return;
  taggerSortDir[key]=!taggerSortDir[key];
  const rows=Object.values(taggerRows);
  rows.sort((a,b)=>{
    const av=String(a[key]||'');const bv=String(b[key]||'');
    return taggerSortDir[key]?av.localeCompare(bv):bv.localeCompare(av);
  });
  const tbody=document.getElementById('taggerBody');tbody.innerHTML='';
  rows.forEach(r=>{const tr=document.createElement('tr');tr.id=r.id;tr.innerHTML=rowHtml(r);tbody.appendChild(tr);});
}

// ─── TAGGER RUN ──────────────────────────────────────────
function runTagger(){
  const path=document.getElementById('music-path').value.trim();
  if(!path){setStatus('Music folder not set',true);return;}
  if(!Object.keys(taggerRows).length){
    // Scan first, then run
    setStatus('Scanning folder…');
    window.pywebview.api.get_file_list(path).then(files=>{
      if(!files||!files.length){setStatus('No audio files found',true);return;}
      taggerRows={};
      document.getElementById('taggerBody').innerHTML='';
      files.forEach(f=>addTaggerRow(f,'pending'));
      _doRun(path);
    });
    return;
  }
  _doRun(path);
}

function _doRun(path){
  logLines=[];
  document.getElementById('runBtn').disabled=true;
  document.getElementById('stopBtn').disabled=false;
  document.getElementById('stopBtn').textContent='⏹ Stop';
  document.getElementById('taggerProgress').style.width='0%';
  Object.keys(taggerRows).forEach(p=>updateTaggerRow(p,{status:'pending',rg:'—',dr:'—'}));
  const opts={music_root:path,lyrics:cbState['cb-lyrics'],genre:cbState['cb-genre'],
    replaygain:cbState['cb-rg'],dr:cbState['cb-dr'],year:cbState['cb-year'],overwrite:cbState['cb-overwrite']};
  window.pywebview.api.run_tagger(opts);
}

function stopTagger(){
  window.pywebview.api.stop_tagger();
  document.getElementById('stopBtn').disabled=true;
  document.getElementById('stopBtn').textContent='⏹ Stopping…';
}

function onLog(msg,type){appendLog(msg,type)}
function onProgress(pct){document.getElementById('taggerProgress').style.width=Math.round(pct*100)+'%'}
function onStatus(msg,isErr){setStatus(msg,isErr||false)}
function onRowUpdate(path,updates){updateTaggerRow(path,updates)}
function onTaggerDone(){
  document.getElementById('runBtn').disabled=false;
  document.getElementById('stopBtn').disabled=true;
  document.getElementById('stopBtn').textContent='⏹ Stop';
  document.getElementById('taggerProgress').style.width='100%';
}

// ─── CHECKER ─────────────────────────────────────────────
let checkerData=[];let checkerSortDir={};
const VERDICT_TIPS={
  hires:'FLAD: lossless source confirmed. Spectrum shows real content above 22kHz — genuine Hi-Res recording.',
  cd:'16bit/44.1kHz container. Standard CD quality detected. No upscaling artifacts.',
  upscale:'FLAD: lossless source. But spectrum shows almost no energy above 22kHz — likely upscaled from CD master.',
  suspicious:'Low high-frequency content (below 16kHz). Possible lossy upscale or heavily compressed/processed master.',
  fake:'FLAD model detected lossy artifacts in spectrum. MP3/AAC/Opus codec fingerprint identified — fake lossless file.',
  error:'Analysis could not be completed for this file.',
};
const VERDICT_MAP={
  hires:['b-ok','Genuine Hi-Res'],cd:['b-cd','Proper CD'],
  upscale:['b-warn','CD upscale'],suspicious:['b-susp','Low HF content'],
  fake:['b-err','Fake lossless'],error:['','Error'],
};

function onCheckerRow(row){checkerData.push(row);renderCheckerTable()}
function onCheckerProgress(pct){document.getElementById('checkerProgress').style.width=Math.round(pct*100)+'%'}
function onCheckerDone(summary){
  document.getElementById('checkerSummary').innerHTML=summary;
  document.getElementById('scanBtn').disabled=false;
  document.getElementById('checkerProgress').style.width='100%';
}

function renderCheckerTable(){
  const tbody=document.getElementById('checkerBody');
  tbody.innerHTML=checkerData.map(r=>{
    const [cls,label]=VERDICT_MAP[r.verdict_key]||['',r.verdict];
    const tip=VERDICT_TIPS[r.verdict_key]||'';
    const badge=cls?`<span class="badge ${cls}" data-tip="${escHtml(tip)}">${escHtml(label)}</span>`:escHtml(label);
    return `<tr><td title="${escHtml(r.file)}">${escHtml(r.file)}</td><td>${escHtml(r.source)}</td><td>${escHtml(r.container)}</td><td style="text-align:center;font-family:var(--mono)">${escHtml(r.dr)}</td><td>${badge}</td></tr>`;
  }).join('');
}

function sortChecker(col){
  const keys=['file','source','container','dr','verdict'];const key=keys[col];
  checkerSortDir[key]=!checkerSortDir[key];
  checkerData.sort((a,b)=>{const av=String(a[key]||'');const bv=String(b[key]||'');return checkerSortDir[key]?av.localeCompare(bv):bv.localeCompare(av);});
  renderCheckerTable();
}

function runChecker(){
  const path=document.getElementById('checker-path').value.trim();
  if(!path)return;
  document.getElementById('checkerBody').innerHTML='';
  document.getElementById('checkerSummary').textContent='Scanning…';
  document.getElementById('scanBtn').disabled=true;
  document.getElementById('checkerProgress').style.width='0%';
  checkerData=[];
  window.pywebview.api.run_checker(path);
}

function openCheckerLog(){window.pywebview.api.open_log()}

// ─── SETTINGS ────────────────────────────────────────────
function saveSettings(){
  const data={
    genius_token:document.getElementById('genius-token').value.trim(),
    lastfm_key:document.getElementById('lastfm-key').value.trim(),
    log_path:document.getElementById('log-path').value.trim(),
    flad_dir:document.getElementById('flad-dir').value.trim(),
    music_root:document.getElementById('settings-music').value.trim(),
  };
  window.pywebview.api.save_settings(data).then(()=>{
    appendLog('✓ Settings saved','ok');
    setStatus('Settings saved');
    // Sync music-path
    if(data.music_root){
      document.getElementById('music-path').value=data.music_root;
    }
  });
}

function openConfig(){window.pywebview.api.open_config()}

function installFlad(statusId){
  const btn=event.target;
  btn.disabled=true;
  const statusEl=document.getElementById(statusId);
  if(statusEl){statusEl.style.display='block';statusEl.textContent='Installing FLAD… this may take a minute';}
  window.pywebview.api.install_flad().then(result=>{
    if(statusEl){statusEl.textContent=result.message;statusEl.style.color=result.ok?'#30d158':'#ff453a';}
    btn.disabled=false;
    if(result.ok){
      document.getElementById('flad-dir').value=result.path;
      document.getElementById('fladBanner').classList.add('hidden');
    }
  });
}

function syncScanBtn(){
  const path=document.getElementById('checker-path').value.trim();
  document.getElementById('scanBtn').disabled=!path;
}

function loadSettings(cfg){
  // Only fill if value is non-empty and not placeholder
  // Set version from Python
  if(cfg._app_version){
    document.querySelectorAll('.app-version').forEach(el=>el.textContent=cfg._app_version);
  }
  if(cfg.GENIUS_TOKEN&&cfg.GENIUS_TOKEN!=='YOUR_GENIUS_API_TOKEN'&&cfg.GENIUS_TOKEN.length>4)
    document.getElementById('genius-token').value=cfg.GENIUS_TOKEN;
  if(cfg.LASTFM_KEY&&cfg.LASTFM_KEY!=='YOUR_LASTFM_API_KEY'&&cfg.LASTFM_KEY.length>4)
    document.getElementById('lastfm-key').value=cfg.LASTFM_KEY;
  if(cfg.LOG_PATH) document.getElementById('log-path').value=cfg.LOG_PATH;
  if(cfg.FLAD_DIR) document.getElementById('flad-dir').value=cfg.FLAD_DIR;
  if(cfg.MUSIC_ROOT&&cfg.MUSIC_ROOT.trim()){
    document.getElementById('music-path').value=cfg.MUSIC_ROOT;
    document.getElementById('settings-music').value=cfg.MUSIC_ROOT;
    const cp=document.getElementById('checker-path');
    if(!cp.value.trim())cp.value=cfg.MUSIC_ROOT;
    // Enable scan button if checker path is set
    const scanBtn=document.getElementById('scanBtn');
    if(scanBtn && cp.value.trim()) scanBtn.disabled=false;
  }
  // Check FLAD availability
  window.pywebview.api.check_flad().then(ok=>{
    if(!ok)document.getElementById('fladBanner').classList.remove('hidden');
  });
}

// ─── CHECK UPDATES ──────────────────────────────────────
let _latestRelease = null;

function checkUpdates(){
  const btn=document.getElementById('checkUpdBtn');
  const status=document.getElementById('updateStatus');
  const dlBtn=document.getElementById('downloadUpdateBtn');
  btn.disabled=true;
  dlBtn.style.display='none';
  status.textContent='Checking GitHub…';
  window.pywebview.api.check_updates().then(r=>{
    btn.disabled=false;
    if(r.error){
      status.innerHTML='❌ '+escHtml(r.error);
      return;
    }
    _latestRelease = r;
    const current=document.querySelector('.app-version').textContent||'v1.0';
    const latest=r.latest_version||'unknown';
    const hasUpdate = latest !== current && latest !== 'unknown';
    let lines=[];
    lines.push('Current version: <b>'+escHtml(current)+'</b>');
    lines.push('Latest on GitHub: <b>'+escHtml(latest)+'</b>');
    if(hasUpdate){
      lines.push('<span style="color:#30d158">✅ Update available!</span>');
      dlBtn.style.display='inline-block';
    } else {
      lines.push('<span style="color:#30d158">✅ You are up to date</span>');
    }
    if(r.release_notes) lines.push('<br><i style="color:var(--text-muted)">'+escHtml(r.release_notes.slice(0,200))+'…</i>');
    status.innerHTML=lines.join('<br>');
  }).catch(e=>{btn.disabled=false;status.textContent='Error: '+e;});
}

function downloadUpdate(){
  if(_latestRelease && _latestRelease.release_url){
    window.pywebview.api.open_url(_latestRelease.release_url);
  }
}

// ─── FLOAT TOOLTIP ───────────────────────────────────────
const floatTip=document.getElementById('floatTip');
document.addEventListener('mouseover',e=>{
  const b=e.target.closest('.badge[data-tip]');
  if(b&&b.dataset.tip){floatTip.textContent=b.dataset.tip;floatTip.style.display='block';}
});
document.addEventListener('mousemove',e=>{
  if(floatTip.style.display==='block'){
    floatTip.style.left=Math.min(e.clientX+12,window.innerWidth-300)+'px';
    floatTip.style.top=Math.max(e.clientY-floatTip.offsetHeight-8,4)+'px';
  }
});
document.addEventListener('mouseout',e=>{if(e.target.closest('.badge[data-tip]'))floatTip.style.display='none';});

// ─── COLUMN RESIZE ───────────────────────────────────────
let _resizing = null;

function startResize(e, handle) {
  e.preventDefault();
  e.stopPropagation(); // prevent sort trigger
  const th = handle.parentElement;
  const startX = e.clientX;
  const startW = th.offsetWidth;
  handle.classList.add('active');
  _resizing = { th, startX, startW, handle };

  function onMove(e) {
    if (!_resizing) return;
    const delta = e.clientX - _resizing.startX;
    const newW = Math.max(40, _resizing.startW + delta);
    _resizing.th.style.width = newW + 'px';
  }

  function onUp() {
    if (_resizing) _resizing.handle.classList.remove('active');
    _resizing = null;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

// ─── UTILS ───────────────────────────────────────────────
function escHtml(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Init
window.addEventListener('pywebviewready',()=>{
  window.pywebview.api.get_config().then(cfg=>loadSettings(cfg));
});
</script>
</body>
</html>
"""


# ============================================================
# PYTHON API
# ============================================================

def interpret_error(err: str, context: str = '') -> str:
    """Convert cryptic error messages into human-readable explanations."""
    e = err.lower()
    # Genius API errors
    if '401' in err:
        return f'❌ Genius: Invalid or expired token (401). Go to Settings → update Genius token at genius.com/api-clients'
    if '403' in err:
        return f'❌ Genius: Access denied (403). Your token may have been revoked.'
    if '429' in err:
        return f'❌ Genius: Rate limit exceeded (429). Too many requests — wait a few minutes and retry.'
    if '404' in err and 'genius' in context:
        return f'❌ Genius: Song not found (404).'
    if '500' in err or '502' in err or '503' in err:
        return f'❌ Server error ({err[:3]}). Genius/Last.fm may be down — try again later.'
    if 'unexpected response status' in e:
        code = ''.join(c for c in err if c.isdigit())[:3]
        msgs = {'401':'Invalid token — update in Settings','403':'Access denied','404':'Not found',
                '429':'Rate limit — wait and retry','500':'Server error'}
        hint = msgs.get(code, 'unexpected server response')
        return f'❌ API error {code}: {hint}'
    # Network errors
    if 'connection reset' in e or 'connection refused' in e:
        return f'❌ Network: Connection reset. Check internet connection.'
    if 'timed out' in e or 'timeout' in e:
        return f'❌ Network: Request timed out. Check internet connection or try again.'
    if 'name or service not known' in e or 'nodename nor servname' in e:
        return f'❌ Network: DNS error — cannot reach server. Check internet connection.'
    if 'ssl' in e or 'certificate' in e:
        return f'❌ SSL error: Certificate verification failed. Try reinstalling Python certificates.'
    # File errors
    if 'permission denied' in e:
        return f'❌ Permission denied: Cannot write to file. Check folder permissions.'
    if 'no such file' in e or 'not found' in e:
        return f'❌ File not found: {err[:80]}'
    if 'disk full' in e or 'no space' in e:
        return f'❌ Disk full: Free up space and retry.'
    # rsgain / ffmpeg
    if 'rsgain' in e:
        return f'❌ rsgain error: {err[:80]}'
    if 'ffmpeg' in e:
        return f'❌ ffmpeg error: {err[:80]}'
    # Last.fm
    if 'lastfm' in context or 'last.fm' in e:
        return f'❌ Last.fm error: {err[:80]}. Check your API key in Settings.'
    # Generic
    return f'❌ Error: {err[:120]}'


class Api:
    def __init__(self, window_ref):
        self._win = window_ref
        self._stop = False
        self._log_path = None

    def _js(self, fn, *args):
        arg_str = ', '.join(json.dumps(a) for a in args)
        self._win.evaluate_js(f'{fn}({arg_str})')

    def get_config(self):
        cfg = load_config()
        # Re-read version fresh each time to catch bundled version.txt
        cfg['_app_version'] = get_app_version()
        return cfg

    def browse_folder(self):
        result = self._win.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
        return result[0] if result else None

    def close_app(self):      self._win.destroy()
    def minimize_app(self):   self._win.minimize()
    def toggle_maximize(self):
        if self._win.maximized: self._win.restore()
        else: self._win.maximize()

    def save_music_root(self, path: str):
        """Save MUSIC_ROOT immediately when folder is changed."""
        cfg = load_config()
        cfg['MUSIC_ROOT'] = path
        save_config(cfg)

    def save_settings(self, data: dict):
        cfg = load_config()
        # Only update non-empty values
        if data.get('genius_token'): cfg['GENIUS_TOKEN'] = data['genius_token']
        if data.get('lastfm_key'):   cfg['LASTFM_KEY']   = data['lastfm_key']
        if 'log_path'   in data:     cfg['LOG_PATH']     = data['log_path']
        if 'flad_dir'   in data:     cfg['FLAD_DIR']     = data['flad_dir']
        if data.get('music_root'):   cfg['MUSIC_ROOT']   = data['music_root']
        save_config(cfg)

    def open_config(self):
        """Open config.txt in default text editor."""
        subprocess.run(['open', CONFIG_PATH])

    def stop_tagger(self):
        self._stop = True

    def check_flad(self) -> bool:
        """Returns True if FLAD model is available (bundled or installed)."""
        return bool(get_flad_dir())

    def reveal_in_finder(self, path: str):
        """Reveal file in macOS Finder."""
        subprocess.run(['open', '-R', path])

    def get_file_list(self, path: str):
        """Scan folder and return file info list with existing tags."""
        files = scan_audio_files(path)
        result = []
        for fp in files:
            ext = fp.lower().rsplit('.', 1)[-1]
            year_raw = read_year(fp) or ''
            year_val = fix_year(year_raw) if year_raw else '—'
            has_l = has_lyrics(fp)

            genre_val = ''
            try:
                if ext == 'mp3':
                    a = MP3(fp, ID3=ID3); tcon = (a.tags or {}).get('TCON')
                    genre_val = str(tcon.text[0]) if tcon else ''
                elif ext == 'flac':
                    genre_val = FLAC(fp).get('genre', [''])[0]
                elif ext == 'm4a':
                    v = (MP4(fp).tags or {}).get('\xa9gen'); genre_val = v[0] if v else ''
                elif ext == 'ogg':
                    genre_val = OggVorbis(fp).get('genre', [''])[0]
            except Exception: pass

            dr_val = '—'
            try:
                if ext == 'mp3':
                    a = MP3(fp, ID3=ID3)
                    for t in (a.tags or {}).values():
                        if isinstance(t, TXXX) and t.desc == 'DYNAMIC_RANGE':
                            dr_val = f'DR{t.text[0]}'; break
                elif ext == 'flac':
                    v = FLAC(fp).get('DYNAMIC_RANGE', [None])[0]
                    if v: dr_val = f'DR{v}'
                elif ext == 'm4a':
                    v = (MP4(fp).tags or {}).get('----:com.apple.iTunes:DYNAMIC_RANGE')
                    if v: dr_val = f'DR{v[0].decode()}'
                elif ext == 'ogg':
                    v = OggVorbis(fp).get('DYNAMIC_RANGE', [None])[0]
                    if v: dr_val = f'DR{v}'
            except Exception: pass

            rg_val = '—'
            try:
                if ext == 'mp3':
                    t = EasyID3(fp); v = t.get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
                elif ext == 'flac':
                    v = FLAC(fp).get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
                elif ext == 'ogg':
                    v = OggVorbis(fp).get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
            except Exception: pass

            result.append({
                'path': fp, 'file': os.path.basename(fp),
                'year': year_val, 'lyrics': has_l,
                'genre': genre_val, 'dr': dr_val, 'rg': rg_val,
            })
        return result

    def run_tagger(self, opts: dict):
        def worker():
            try:
                music_root = opts['music_root']
                cfg = load_config()
                genius_token = cfg.get('GENIUS_TOKEN', '')
                lastfm_key   = cfg.get('LASTFM_KEY', '')
                log_path     = cfg.get('LOG_PATH') or os.path.join(music_root, 'tagger_log.txt')
                self._log_path = log_path
                self._stop = False

                def log(msg, t='normal'): self._js('onLog', msg, t)
                def status(msg, err=False): self._js('onStatus', msg, err)
                def row(path, updates): self._js('onRowUpdate', path, updates)

                # Step 1: scan folder if needed
                status('Scanning folder…')
                log(f'Scanning {music_root}…', 'info')
                files = scan_audio_files(music_root)
                total = len(files)
                if not total:
                    status('No audio files found', True)
                    self._js('onTaggerDone')
                    return
                log(f'Found {total} files', 'ok')

                genius_client = None
                if opts.get('lyrics'):
                    import lyricsgenius as lg
                    genius_client = lg.Genius(genius_token)
                    genius_client.verbose = False
                    genius_client.remove_section_headers = False

                # ReplayGain — whole folder at once
                if opts.get('replaygain') and not self._stop:
                    status('Applying ReplayGain…')
                    log('Applying ReplayGain…', 'info')
                    try:
                        res = subprocess.run(
                            [get_rsgain_path(), 'easy', music_root],
                            capture_output=True, text=True
                        )
                        if res.returncode == 0:
                            log('✓ ReplayGain done', 'ok')
                            # Parse per-track gain from rsgain output
                            # Strip ANSI escape codes first
                            import re as _re
                            clean_out = _re.sub(r'\x1b\[[0-9;]*m', '', res.stdout)
                            current_file = None
                            for line in clean_out.splitlines():
                                line = line.strip()
                                if line.startswith('Track:'):
                                    current_file = line.replace('Track:', '').strip()
                                elif line.startswith('Gain:') and current_file:
                                    # "Gain:  -13.06 dB" → "-13.06"
                                    parts = line.replace('Gain:', '').strip().split()
                                    gain = parts[0] if parts else '?'
                                    row(current_file, {'rg': f'{gain} dB'})
                                    log(f'  RG {os.path.basename(current_file)}: {gain} dB', 'dim')
                                    current_file = None  # reset to avoid duplicate
                        else:
                            err_msg = res.stderr.strip()
                            log(interpret_error(err_msg, 'rsgain'), 'err')
                            status('ReplayGain failed — see log for details', True)
                    except FileNotFoundError:
                        log('✗ rsgain not found — bundled binary missing', 'err')
                        status('rsgain not found', True)

                retry_files = []

                for idx, filepath in enumerate(files):
                    if self._stop:
                        log('⛔ Stopped by user', 'err')
                        status('Stopped by user')
                        break

                    fname = os.path.basename(filepath)
                    title, artist = get_tags(filepath)
                    self._js('onProgress', (idx + 1) / total)
                    row(filepath, {'status': 'running'})
                    updates = {'status': 'ok'}
                    had_error = False

                    # Year fix
                    if opts.get('year'):
                        raw = read_year(filepath)
                        if raw:
                            fixed = fix_year(raw)
                            if fixed != raw:
                                write_year(filepath, fixed)
                                log(f'📅 {fname}: {raw} → {fixed}')
                                updates['year'] = fixed
                            else:
                                updates['year'] = raw
                        else:
                            updates['year'] = '—'

                    # DR
                    if opts.get('dr'):
                        status(f'Analyzing DR — {fname}')
                        try:
                            dr = calculate_dr14(filepath)
                            if dr:
                                write_dr_tag(filepath, dr)
                                updates['dr'] = f'DR{dr["dr"]}'
                                log(f'🎛 {fname}: DR{dr["dr"]}')
                            else:
                                updates['dr'] = '—'
                        except Exception as e:
                            msg = interpret_error(str(e))
                            log(f'{msg} — {fname}', 'err')
                            status(f'DR error: {fname}', True)
                            had_error = True

                    # Lyrics + Genre
                    if opts.get('lyrics') and title and artist:
                        already = has_lyrics(filepath)
                        if already and not opts.get('overwrite'):
                            log(f'⏩ {fname}: lyrics present', 'dim')
                            updates['lyrics'] = 'Yes'
                        else:
                            status(f'Fetching lyrics — {artist} – {title}')
                            title_clean = re.sub(r'\s*\(.*?\)\s*', ' ', title).strip()
                            has_parens = title_clean != title
                            song = None
                            try:
                                song = genius_client.search_song(title, artist)
                                if (not song or not song.lyrics) and has_parens:
                                    log(f'🔍 {fname}: retrying without parentheses…', 'dim')
                                    song = genius_client.search_song(title_clean, artist)
                            except Exception as e:
                                err = str(e)
                                if 'reset' in err.lower() or 'timed out' in err.lower():
                                    retry_files.append(filepath)
                                    log(interpret_error(err, 'genius'), 'err')
                                    log(f'  → Will retry: {fname}', 'dim')
                                    status(f'Network error — will retry: {fname}', True)
                                else:
                                    log(interpret_error(err, 'genius'), 'err')
                                    status(interpret_error(err, 'genius')[:60], True)
                                    had_error = True

                            if song and song.lyrics:
                                write_lyrics(filepath, clean_lyrics(song.lyrics))
                                log(f'✓ {fname}: lyrics added', 'ok')
                                updates['lyrics'] = 'Yes'
                            elif song is not None:
                                log(f'✗ {fname}: lyrics not found', 'err')
                                updates['lyrics'] = 'No'

                            # Genre
                            if opts.get('genre') and title and artist:
                                already_genre = has_genre(filepath)
                                if not already_genre or opts.get('overwrite'):
                                    status(f'Fetching genre — {artist}')
                                    try:
                                        genre = get_genre_lastfm(artist, title, lastfm_key)
                                        if not genre and song:
                                            try:
                                                tag = song.to_dict().get('primary_tag', {})
                                                genre = (tag.get('name', '') or '').title() or None
                                            except Exception: pass
                                        if genre:
                                            write_genre(filepath, genre)
                                            log(f'🎸 {fname}: genre → {genre}', 'ok')
                                            updates['genre'] = genre
                                        else:
                                            status(f'Genre not found: {artist}', True)
                                    except Exception as e:
                                        msg = interpret_error(str(e), 'lastfm')
                                        log(msg, 'err')
                                        status(msg[:60], True)

                    if had_error: updates['status'] = 'err'
                    row(filepath, updates)

                # Retry
                if retry_files and not self._stop:
                    log(f'\n🔁 Retrying {len(retry_files)} files…', 'info')
                    status('Retrying failed files…')
                    time.sleep(3)
                    for filepath in retry_files:
                        if self._stop: break
                        fname = os.path.basename(filepath)
                        title, artist = get_tags(filepath)
                        if title and artist:
                            try:
                                song = genius_client.search_song(title, artist)
                                if song and song.lyrics:
                                    write_lyrics(filepath, clean_lyrics(song.lyrics))
                                    log(f'✓ {fname}: lyrics added on retry', 'ok')
                                    row(filepath, {'lyrics': 'Yes', 'status': 'ok'})
                                else:
                                    row(filepath, {'status': 'err'})
                            except Exception:
                                log(f'✗ {fname}: retry failed', 'err')
                                row(filepath, {'status': 'err'})

                log('\n✅ All done!', 'ok')
                status('Done')
            except Exception as e:
                msg = interpret_error(str(e))
                self._js('onLog', msg, 'err')
                self._js('onStatus', msg[:80], True)
            finally:
                self._js('onTaggerDone')

        threading.Thread(target=worker, daemon=True).start()

    def run_checker(self, path: str):
        def worker():
            try:
                files = [f for f in scan_audio_files(path) if f.lower().endswith('.flac')]
                total = len(files)
                if not total:
                    self._js('onCheckerDone', 'No FLAC files found')
                    return

                cfg = load_config()
                log_path = cfg.get('LOG_PATH') or os.path.join(path, 'flac_checker_log.txt')
                self._log_path = log_path
                log_lines = [
                    'FLAC Checker Log',
                    f'Folder: {path}',
                    f'Date: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
                    f'Files: {total}',
                ]

                counts = {}
                for idx, filepath in enumerate(files):
                    file_log = []
                    result = run_flac_check(filepath, file_log)
                    log_lines.extend(file_log)
                    key = result['verdict_key']
                    counts[key] = counts.get(key, 0) + 1
                    self._js('onCheckerRow', result)
                    self._js('onCheckerProgress', (idx + 1) / total)

                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(log_lines))

                parts = []
                labels = [('hires','Genuine Hi-Res'),('cd','Proper CD'),
                          ('upscale','CD upscale'),('suspicious','Low HF content'),('fake','Fake')]
                for k, label in labels:
                    if counts.get(k):
                        parts.append(f'<b>{label}:</b> {counts[k]}')
                summary = f'Total: <b>{total}</b> &nbsp;|&nbsp; ' + ' &nbsp;|&nbsp; '.join(parts)
                self._js('onCheckerDone', summary)
            except Exception as e:
                self._js('onCheckerDone', f'Error: {e}')

        threading.Thread(target=worker, daemon=True).start()

    def check_updates(self) -> dict:
        """Check GitHub for latest AudioTagger release."""
        import urllib.request, json, ssl
        ssl_ctx = ssl.create_default_context()
        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        try:
            url = 'https://api.github.com/repos/Nik-Grish/Audio-Tagger/releases/latest'
            req = urllib.request.Request(url, headers={'User-Agent': f'AudioTagger/{APP_VERSION}'})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as r:
                data = json.loads(r.read())

            tag = data.get('tag_name', 'unknown')
            body = data.get('body', '')
            # Find DMG asset download URL
            dmg_url = None
            for asset in data.get('assets', []):
                if asset['name'].endswith('.dmg'):
                    dmg_url = asset['browser_download_url']
                    break

            return {
                'latest_version': tag,
                'release_url': data.get('html_url', 'https://github.com/Nik-Grish/Audio-Tagger/releases'),
                'dmg_url': dmg_url,
                'release_notes': body[:300] if body else '',
            }
        except Exception as e:
            return {'error': str(e)[:100]}

    def open_url(self, url: str):
        """Open URL in default browser."""
        subprocess.run(['open', url])

    def open_log(self):
        if self._log_path and os.path.exists(self._log_path):
            subprocess.run(['open', self._log_path])
        else:
            self._js('onLog', 'No log file yet — run a scan first', 'err')

    def install_flad(self):
        """Clone FLAD repo and download model."""
        import ssl
        import urllib.request
        flad_dir = os.path.join(os.path.expanduser('~'), 'FLAD')
        model_dir = os.path.join(flad_dir, 'models')
        model_path = os.path.join(model_dir, 'flad.onnx')

        # SSL context that works with Python.org builds which lack system certs
        ssl_ctx = ssl.create_default_context()
        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        def download(url, dest):
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_ctx)
            )
            with opener.open(url) as r, open(dest, 'wb') as f:
                f.write(r.read())

        try:
            if not os.path.exists(flad_dir):
                res = subprocess.run(
                    ['git', 'clone', 'https://github.com/Sg4Dylan/FLAD.git', flad_dir],
                    capture_output=True, text=True
                )
                if res.returncode != 0:
                    return {'ok': False, 'message': f'git clone failed: {res.stderr[:100]}', 'path': ''}
            else:
                subprocess.run(['git', '-C', flad_dir, 'pull'], capture_output=True)

            os.makedirs(model_dir, exist_ok=True)
            if not os.path.exists(model_path):
                model_url = 'https://github.com/Sg4Dylan/FLAD/releases/download/v0.1/flad.onnx'
                download(model_url, model_path)

            cfg = load_config()
            cfg['FLAD_DIR'] = flad_dir
            save_config(cfg)
            return {'ok': True, 'message': f'FLAD installed to {flad_dir}', 'path': flad_dir}
        except Exception as e:
            return {'ok': False, 'message': f'Error: {str(e)[:120]}', 'path': ''}


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    window = webview.create_window(
        'Audio Tagger',
        html=HTML,
        width=920,
        height=720,
        min_size=(700, 560),
        frameless=False,
        easy_drag=False,
        background_color='#060614',
    )
    api = Api(window)
    for method in [
        api.get_config, api.browse_folder, api.close_app,
        api.minimize_app, api.toggle_maximize,
        api.save_settings, api.save_music_root,
        api.open_config, api.open_log,
        api.stop_tagger, api.check_flad,
        api.reveal_in_finder, api.get_file_list,
        api.run_tagger, api.run_checker, api.install_flad, api.check_updates, api.open_url,
    ]:
        window.expose(method)
    webview.start(debug=False)