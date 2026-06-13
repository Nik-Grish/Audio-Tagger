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

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.txt")
SUPPORTED_EXTS = {"mp3", "flac", "m4a", "ogg", "wav", "aiff"}
LASTFM_BASE = 'https://ws.audioscrobbler.com/2.0/'
_NON_GENRE_TAGS = {
    'seen live', 'favorite', 'love', 'awesome', 'cool', 'great', 'beautiful',
    'amazing', 'best', 'favourite', 'owned', 'albums i own',
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

def _pick_genre(tags: list):
    for tag in tags:
        name = tag.get('name', '').strip()
        count = int(tag.get('count', 0))
        if count < 5: continue
        if name.lower() in _NON_GENRE_TAGS: continue
        if len(name) < 2 or len(name) > 40: continue
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
    cmd = ['/opt/homebrew/bin/ffmpeg', '-i', filepath, '-f', 'f32le', '-ac', '2',
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

FLAD_DIR = '/Users/nikitagrisacev/FLAD'

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

    if FLAD_DIR and os.path.exists(FLAD_DIR):
        try:
            orig_cwd = os.getcwd()
            orig_path = _sys.path[:]
            os.chdir(FLAD_DIR)
            if FLAD_DIR not in _sys.path:
                _sys.path.insert(0, FLAD_DIR)
            from flad.eval import FLAD as FLADModel
            flad = FLADModel()

            from flad import utils
            import numpy as _np

            r_map = ['FLAC', 'AAC', 'mp3', 'Opus']
            y_s = utils.get_side(filepath)
            os.makedirs('temp', exist_ok=True)
            utils.get_spectrum(y_s, 0, 'temp', max=20)
            spectrum_list = utils.get_file_list('temp', ext='.jpg')

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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Audio Tagger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --glass-bg:rgba(255,255,255,0.08);
  --glass-border:rgba(255,255,255,0.18);
  --glass-input:rgba(255,255,255,0.07);
  --text:rgba(255,255,255,0.92);
  --text-muted:rgba(255,255,255,0.45);
  --sep:rgba(255,255,255,0.1);
  --log-bg:rgba(0,0,0,0.35);
  --sidebar-w:176px;
  --radius:14px;
  --font:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
  --mono:'Menlo','SF Mono',monospace;
}
body.light{
  --glass-bg:rgba(255,255,255,0.52);
  --glass-border:rgba(255,255,255,0.85);
  --glass-input:rgba(255,255,255,0.65);
  --text:rgba(0,0,0,0.88);
  --text-muted:rgba(0,0,0,0.45);
  --sep:rgba(0,0,0,0.1);
  --log-bg:rgba(0,0,0,0.05);
}
html,body{height:100%;overflow:hidden}
body{font-family:var(--font);background:#060614;color:var(--text);display:flex;flex-direction:column;height:100vh;transition:color .3s}
body.light{background:#dce8fa}

/* ORBS */
.scene{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.orb{position:absolute;border-radius:50%;filter:blur(72px);opacity:.38;transition:opacity .5s}
body.light .orb{opacity:.22}
.o1{width:420px;height:420px;background:#3b82f6;top:-120px;left:-90px}
.o2{width:360px;height:360px;background:#8b5cf6;top:40px;right:-70px}
.o3{width:320px;height:320px;background:#06b6d4;bottom:-60px;left:32%}

/* TITLEBAR */
.titlebar{height:44px;display:flex;align-items:center;padding:0 14px;gap:8px;flex-shrink:0;position:relative;z-index:10;cursor:default}
.dots{display:flex;gap:6px}
.dot{width:12px;height:12px;border-radius:50%;cursor:pointer;transition:filter .15s}
.dot:hover{filter:brightness(1.3)}
.dot-r{background:#ff5f57}.dot-y{background:#ffbd2e}.dot-g{background:#28c840}
.tb-icon{font-size:18px;margin-left:10px;opacity:.7}
.drag-area{width:80px;height:44px;position:absolute;top:0;left:0;-webkit-app-region:drag;-webkit-user-select:none}
.win-title{font-size:13px;font-weight:500;color:var(--text-muted);margin-left:4px}
.theme-toggle{margin-left:auto;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:20px;padding:4px 12px;font-size:12px;color:var(--text-muted);cursor:pointer;display:flex;align-items:center;gap:5px;transition:all .2s;font-family:var(--font)}
.theme-toggle:hover{color:var(--text);background:rgba(255,255,255,0.12)}

/* LAYOUT */
.layout{display:flex;flex:1;overflow:hidden;position:relative;z-index:2;padding:0 12px 12px;gap:10px}

/* SIDEBAR */
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

/* CONTENT */
.content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.panel{display:none;flex-direction:column;height:100%;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:var(--radius);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);padding:14px;overflow:hidden;position:relative}
.panel::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.32),transparent)}
.panel.active{display:flex}

/* FIELDS */
.field-row{display:flex;align-items:center;gap:7px;margin-bottom:9px}
.field-lbl{font-size:12px;color:var(--text-muted);width:86px;flex-shrink:0}
.glass-input{flex:1;background:var(--glass-input);border:1px solid var(--glass-border);border-radius:10px;padding:7px 10px;font-size:12px;color:var(--text);font-family:var(--mono);outline:none;transition:border .2s}
.glass-input:focus{border-color:rgba(10,132,255,.6)}
.glass-btn{background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:9px;padding:7px 12px;font-size:12px;color:var(--text-muted);cursor:pointer;transition:all .15s;white-space:nowrap;font-family:var(--font)}
.glass-btn:hover{background:rgba(255,255,255,0.13);color:var(--text)}
.glass-btn.icon-btn{padding:7px 10px;font-size:14px}
.sep{height:1px;background:var(--sep);margin:9px 0}

/* CHECKBOXES */
.opts-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px}
.opt{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;position:relative}
.glass-check{width:16px;height:16px;border-radius:5px;border:1px solid rgba(255,255,255,0.3);background:rgba(10,132,255,.55);display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .15s;position:relative;overflow:hidden}
.glass-check::after{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:rgba(255,255,255,0.18)}
.glass-check.off{background:rgba(255,255,255,0.07)}
.glass-check svg{position:relative;z-index:1}
.opt-lbl{font-size:13px;color:var(--text)}
.opt-lbl.dim{color:var(--text-muted)}

/* TOOLTIP */
.tip{display:none;position:absolute;left:0;top:calc(100% + 6px);z-index:100;background:rgba(20,20,40,0.96);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:8px 11px;font-size:11px;line-height:1.5;color:rgba(255,255,255,0.85);white-space:normal;width:220px;pointer-events:none;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,0.4)}
body.light .tip{background:rgba(240,245,255,0.97);color:rgba(0,0,0,0.8);border-color:rgba(0,0,0,0.12)}
.opt:hover .tip{display:block}

/* BUTTONS */
.btn-row{display:flex;gap:7px;margin-bottom:9px}
.btn-run{flex:1;background:rgba(10,132,255,.55);border:1px solid rgba(10,132,255,.7);border-radius:12px;padding:8px;font-size:14px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.btn-run::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent)}
.btn-run:hover{filter:brightness(1.15)}.btn-run:disabled{opacity:.45;cursor:not-allowed}
.btn-stop{background:rgba(255,69,58,.55);border:1px solid rgba(255,69,58,.65);border-radius:12px;padding:8px 18px;font-size:14px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.btn-stop::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.15),transparent)}
.btn-stop:hover{filter:brightness(1.15)}.btn-stop:disabled{opacity:.3;cursor:not-allowed}

/* PROGRESS */
.progress-wrap{height:3px;background:rgba(255,255,255,0.09);border-radius:2px;margin-bottom:8px;overflow:hidden;flex-shrink:0}
.progress-fill{height:100%;width:0%;background:linear-gradient(90deg,#0a84ff,#30d158);border-radius:2px;transition:width .3s ease}

/* TAGGER TABLE */
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

/* STATUS BAR */
.statusbar{flex-shrink:0;margin-top:7px;display:flex;align-items:center;gap:8px;min-height:22px}
.status-text{font-size:11px;color:var(--text-muted);cursor:pointer;transition:color .15s;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status-text:hover{color:var(--text);text-decoration:underline}
.status-text.err{color:#ff6b6b}

/* LOG POPUP */
.log-overlay{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,0.5);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);align-items:center;justify-content:center}
.log-overlay.open{display:flex}
.log-popup{background:rgba(18,18,35,0.97);border:1px solid rgba(255,255,255,0.15);border-radius:16px;width:680px;max-height:70vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,0.6)}
body.light .log-popup{background:rgba(240,245,255,0.98);border-color:rgba(0,0,0,0.12)}
.log-popup-header{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--sep)}
.log-popup-title{font-size:13px;font-weight:500;flex:1}
.log-popup-close{background:none;border:none;color:var(--text-muted);font-size:18px;cursor:pointer;padding:0 4px;line-height:1}
.log-popup-close:hover{color:var(--text)}
.log-content{flex:1;overflow-y:auto;padding:12px 14px;font-family:var(--mono);font-size:11px;line-height:1.7}
.log-content::-webkit-scrollbar{width:6px}.log-content::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:3px}
.log-ok{color:#30d158}.log-err{color:#ff453a}.log-info{color:#64d2ff}.log-dim{color:var(--text-muted)}
.log-line{margin:0}

/* CHECKER PANEL */
.verdict-hires{color:#30d158}.verdict-cd{color:#64d2ff}.verdict-upscale{color:#ffd60a}
.verdict-suspicious{color:#ff9f0a}.verdict-fake{color:#ff453a}.verdict-error{color:rgba(255,255,255,.3)}

/* Badges — readable on both themes */
.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;position:relative;cursor:default}
.b-ok{background:rgba(48,209,88,.15);border:1px solid rgba(48,209,88,.4);color:#30d158}
.b-warn{background:rgba(255,214,10,.18);border:1px solid rgba(255,180,0,.55);color:#b8860b}
body.light .b-warn{background:rgba(180,120,0,.18);border-color:rgba(150,100,0,.5);color:#7a5800}
.b-err{background:rgba(255,69,58,.15);border:1px solid rgba(255,69,58,.4);color:#ff453a}
.b-cd{background:rgba(0,100,200,.18);border:1px solid rgba(0,120,220,.5);color:#1a6fbf}
body.light .b-cd{background:rgba(0,80,180,.15);border-color:rgba(0,80,160,.5);color:#0040a0}
.b-susp{background:rgba(255,159,10,.15);border:1px solid rgba(255,159,10,.4);color:#cc7700}
body.light .b-susp{background:rgba(180,100,0,.15);border-color:rgba(150,80,0,.4);color:#7a4400}

/* Badge tooltip */
.badge-tip{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);z-index:150;background:rgba(20,20,40,0.97);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:9px 12px;font-size:11px;line-height:1.6;color:rgba(255,255,255,0.88);white-space:normal;width:260px;pointer-events:none;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,.5);font-weight:400}
body.light .badge-tip{background:rgba(240,245,255,0.98);color:rgba(0,0,0,0.82);border-color:rgba(0,0,0,.12)}
#floatTip{display:none;position:fixed;z-index:999;background:rgba(20,20,40,0.97);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:9px 12px;font-size:11px;line-height:1.6;color:rgba(255,255,255,0.88);max-width:280px;pointer-events:none;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,.5)}
body.light #floatTip{background:rgba(240,245,255,0.98);color:rgba(0,0,0,0.82);border-color:rgba(0,0,0,.12)}
.opt:hover .tip{display:block}

.checker-progress-wrap{height:3px;background:rgba(255,255,255,0.08);border-radius:2px;margin-top:8px;overflow:hidden;flex-shrink:0}
.checker-progress-fill{height:100%;width:0%;background:linear-gradient(90deg,#0a84ff,#30d158);transition:width .3s ease}
.summary{font-size:11px;color:var(--text-muted);padding:6px 0 0;flex-shrink:0}
.summary b{color:var(--text);font-weight:500}

/* SETTINGS */
.settings-note{font-size:12px;color:var(--text-muted);line-height:1.6;background:var(--glass-input);border:1px solid var(--sep);border-radius:10px;padding:10px 12px;margin-top:10px}
.settings-note a{color:#64d2ff}
body.light .settings-note a{color:#0066cc}
.save-btn{display:inline-block;background:rgba(10,132,255,.55);border:1px solid rgba(10,132,255,.7);border-radius:10px;padding:8px 20px;font-size:13px;font-weight:600;color:white;cursor:pointer;transition:all .15s;font-family:var(--font);position:relative;overflow:hidden}
.save-btn::before{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent)}
.save-btn:hover{filter:brightness(1.15)}
</style>
</head>
<body id="body">

<div class="scene">
  <div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div>
</div>

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

<div class="titlebar">
  <div class="drag-area" id="dragArea"></div>
  <span class="tb-icon">🎧</span>
  <span class="win-title">Audio Tagger</span>
  <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn">🌙 Dark</button>
</div>

<div class="layout">
  <nav class="sidebar">
    <div class="nav-item active" onclick="showPanel('tagger',this)">
      <span class="nav-icon">🎵</span> Tagger
    </div>
    <div class="nav-item" onclick="showPanel('checker',this)">
      <span class="nav-icon">🔍</span> FLAC Checker
    </div>
    <div class="sidebar-sep"></div>
    <div class="nav-item" onclick="showPanel('settings',this)">
      <span class="nav-icon">⚙️</span> Settings
    </div>
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
        <input class="glass-input" id="music-path" placeholder="/path/to/music" onchange="loadFileList()">
        <button class="glass-btn icon-btn" onclick="browseFolder('music-path')" title="Browse">📁</button>
        <button class="glass-btn icon-btn" onclick="loadFileList()" title="Rescan folder">🔄</button>
      </div>

      <div class="sep"></div>

      <div class="opts-grid">

        <div class="opt" onclick="toggleCb('cb-lyrics')">
          <div class="glass-check" id="cb-lyrics"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Add lyrics</span>
          <div class="tip">Searches Genius API.<br><b>Skips</b> if lyrics tag already present.<br><b>Tries</b> full title first, then without parentheses.<br>Enable "Overwrite" to force-replace.</div>
        </div>

        <div class="opt" onclick="toggleCb('cb-genre')">
          <div class="glass-check" id="cb-genre"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Fetch genre</span>
          <div class="tip">Fetches top tag from Last.fm (track → artist fallback).<br><b>Skips</b> if genre tag already present.<br>Never overwrites existing genre.</div>
        </div>

        <div class="opt" onclick="toggleCb('cb-rg')">
          <div class="glass-check" id="cb-rg"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">ReplayGain</span>
          <div class="tip">Runs <code>rsgain easy</code> on entire folder.<br><b>Always overwrites</b> existing RG tags — rsgain recalculates every time.</div>
        </div>

        <div class="opt" onclick="toggleCb('cb-dr')">
          <div class="glass-check" id="cb-dr"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Dynamic Range</span>
          <div class="tip">Calculates DR14 (Pleasurize Music Foundation standard).<br><b>Always overwrites</b> existing DYNAMIC_RANGE tag.</div>
        </div>

        <div class="opt" onclick="toggleCb('cb-year')">
          <div class="glass-check" id="cb-year"><svg width="10" height="10" viewBox="0 0 10 10"><polyline points="1.5,5 4,7.5 8.5,2.5" fill="none" stroke="white" stroke-width="1.8"/></svg></div>
          <span class="opt-lbl">Fix year tags</span>
          <div class="tip">Strips month/day from date tags.<br><code>2007-05-14</code> → <code>2007</code><br><b>Skips</b> if tag already 4-digit year.</div>
        </div>

        <div class="opt" onclick="toggleCb('cb-overwrite')">
          <div class="glass-check off" id="cb-overwrite"></div>
          <span class="opt-lbl dim">Overwrite all</span>
          <div class="tip">Forces re-fetch of lyrics and genre even if tags are already present.<br>Does not affect ReplayGain or DR (always recalculated).</div>
        </div>

      </div>

      <div class="sep"></div>

      <div class="btn-row">
        <button class="btn-run" id="runBtn" onclick="runTagger()">▶ Run</button>
        <button class="btn-stop" id="stopBtn" onclick="stopTagger()" disabled>⏹ Stop</button>
      </div>

      <div class="progress-wrap"><div class="progress-fill" id="taggerProgress"></div></div>

      <!-- TAGGER TABLE -->
      <div class="tbl-wrap" id="taggerTblWrap">
        <table id="taggerTable">
          <thead>
            <tr>
              <th style="width:32px;text-align:center"> </th>
              <th style="width:28%" onclick="sortTagger(1)">File ↕</th>
              <th style="width:10%;text-align:center" onclick="sortTagger(2)">RG ↕</th>
              <th style="width:10%;text-align:center" onclick="sortTagger(3)">DR ↕</th>
              <th style="width:10%;text-align:center" onclick="sortTagger(4)">Lyrics ↕</th>
              <th style="width:14%;text-align:center" onclick="sortTagger(5)">Year ↕</th>
              <th onclick="sortTagger(6)">Genre ↕</th>
            </tr>
          </thead>
          <tbody id="taggerBody"></tbody>
        </table>
      </div>

      <!-- STATUS BAR -->
      <div class="statusbar">
        <span class="status-text" id="statusText" onclick="openLogPopup()">Ready — click to see log</span>
      </div>
    </div>

    <!-- FLAC CHECKER -->
    <div class="panel" id="panel-checker">
      <div class="field-row" style="margin-bottom:12px">
        <input class="glass-input" id="checker-path" placeholder="/path/to/music">
        <button class="glass-btn" onclick="browseFolder('checker-path')">Browse</button>
        <button class="btn-run" style="flex:none;padding:7px 22px;font-size:13px;border-radius:10px" onclick="runChecker()" id="scanBtn">Scan</button>
        <button class="glass-btn" onclick="openCheckerLog()">Log</button>
      </div>

      <div class="tbl-wrap">
        <table id="checkerTable">
          <thead>
            <tr>
              <th style="width:30%" onclick="sortChecker(0)">File ↕</th>
              <th style="width:14%" onclick="sortChecker(1)">Source ↕</th>
              <th style="width:17%" onclick="sortChecker(2)">Container ↕</th>
              <th style="width:8%;text-align:center" onclick="sortChecker(3)">DR ↕</th>
              <th onclick="sortChecker(4)">Verdict ↕</th>
            </tr>
          </thead>
          <tbody id="checkerBody"></tbody>
        </table>
      </div>

      <div class="summary" id="checkerSummary">Ready to scan</div>
      <div class="checker-progress-wrap"><div class="checker-progress-fill" id="checkerProgress"></div></div>
    </div>

    <!-- SETTINGS -->
    <div class="panel" id="panel-settings">
      <div class="field-row">
        <span class="field-lbl">Genius token</span>
        <input class="glass-input" id="genius-token" type="password" placeholder="Genius API token">
      </div>
      <div class="field-row">
        <span class="field-lbl">Last.fm key</span>
        <input class="glass-input" id="lastfm-key" type="password" placeholder="Last.fm API key">
      </div>
      <div class="field-row">
        <span class="field-lbl">Log path</span>
        <input class="glass-input" id="log-path" placeholder="Empty = saves to music folder">
      </div>
      <div class="sep"></div>
      <button class="save-btn" onclick="saveSettings()">Save</button>
      <div class="settings-note">
        Genius token: <a href="https://genius.com/api-clients">genius.com/api-clients</a><br>
        Last.fm key: <a href="https://www.last.fm/api/account/create">last.fm/api/account/create</a><br>
        Settings are saved to <code>config.txt</code> next to the app.
      </div>
    </div>

  </div>
</div>

<script>
// ─── THEME ───────────────────────────────────────────────
let isDark = true;
function toggleTheme(){
  isDark = !isDark;
  document.getElementById('body').className = isDark ? '' : 'light';
  document.getElementById('themeBtn').textContent = isDark ? '🌙 Dark' : '☀️ Light';
}

// ─── NAV ─────────────────────────────────────────────────
function showPanel(name, el){
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
    const lbl=el.nextElementSibling;
    if(lbl){lbl.classList.remove('dim');}
  } else {
    el.classList.add('off');
    el.innerHTML='';
    const lbl=el.nextElementSibling;
    if(lbl){lbl.classList.add('dim');}
  }
}

// ─── BROWSE ──────────────────────────────────────────────
function browseFolder(inputId){
  window.pywebview.api.browse_folder().then(path=>{
    if(path){
      document.getElementById(inputId).value=path;
      if(inputId==='music-path'){
        document.getElementById('checker-path').value=path;
        loadFileList();
      }
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

// ─── STATUS BAR ──────────────────────────────────────────
function setStatus(msg,isErr){
  const el=document.getElementById('statusText');
  el.textContent=msg+' — click to see log';
  el.className='status-text'+(isErr?' err':'');
}

// ─── TAGGER TABLE ────────────────────────────────────────
let taggerRows={};  // filepath -> row data
let taggerSortDir={};

function loadFileList(){
  const path=document.getElementById('music-path').value.trim();
  if(!path)return;
  taggerRows={};
  document.getElementById('taggerBody').innerHTML='';
  setStatus('Scanning folder...');
  window.pywebview.api.get_file_list(path).then(files=>{
    files.forEach(f=>addTaggerRow(f,'pending'));
    setStatus('Ready — '+files.length+' files');
  });
}

function addTaggerRow(fileInfo, status){
  const id='row-'+btoa(unescape(encodeURIComponent(fileInfo.path))).replace(/[^a-zA-Z0-9]/g,'');
  taggerRows[fileInfo.path]={...fileInfo, status,
    rg: fileInfo.rg || '—',
    dr: fileInfo.dr || '—',
    lyrics: fileInfo.lyrics ? 'Yes' : 'No',
    year: fileInfo.year || '—',
    genre: fileInfo.genre || '—',
    id};

  const tbody=document.getElementById('taggerBody');
  const tr=document.createElement('tr');
  tr.id=id;
  tr.innerHTML=rowHtml(taggerRows[fileInfo.path]);
  tbody.appendChild(tr);
}

function updateTaggerRow(path, updates){
  if(!taggerRows[path])return;
  Object.assign(taggerRows[path], updates);
  const tr=document.getElementById(taggerRows[path].id);
  if(tr) tr.innerHTML=rowHtml(taggerRows[path]);
}

const STATUS_ICON={pending:'○', running:'⏳', ok:'✅', err:'❌', skip:'⏩', skip_tag:'➖'};

function rowHtml(r){
  const icon=STATUS_ICON[r.status]||'⬜';
  const lyricsCls=r.lyrics==='Yes'?'style="color:#30d158"':'style="color:var(--text-muted)"';
  return `<td style="text-align:center;font-size:14px">${icon}</td>
    <td title="${escHtml(r.path)}">${escHtml(r.file)}</td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.rg)}</td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.dr)}</td>
    <td style="text-align:center" ${lyricsCls}>${escHtml(r.lyrics)}</td>
    <td style="text-align:center;font-family:var(--mono);font-size:11px">${escHtml(r.year)}</td>
    <td>${escHtml(r.genre)}</td>`;
}

function sortTagger(col){
  const keys=['status','file','rg','dr','lyrics','year','genre'];
  const key=keys[col];
  taggerSortDir[key]=!taggerSortDir[key];
  const rows=Object.values(taggerRows);
  rows.sort((a,b)=>{
    const av=String(a[key]||''); const bv=String(b[key]||'');
    return taggerSortDir[key]?av.localeCompare(bv):bv.localeCompare(av);
  });
  const tbody=document.getElementById('taggerBody');
  tbody.innerHTML='';
  rows.forEach(r=>{
    const tr=document.createElement('tr');
    tr.id=r.id; tr.innerHTML=rowHtml(r);
    tbody.appendChild(tr);
  });
}

// ─── TAGGER RUN ──────────────────────────────────────────
function runTagger(){
  const path=document.getElementById('music-path').value.trim();
  if(!path){setStatus('Music folder not set',true);return;}
  logLines=[];
  document.getElementById('runBtn').disabled=true;
  document.getElementById('stopBtn').disabled=false;
  document.getElementById('taggerProgress').style.width='0%';

  // Reset all rows to pending
  Object.keys(taggerRows).forEach(p=>updateTaggerRow(p,{status:'pending',rg:'—',dr:'—'}));

  const opts={
    music_root:path,
    lyrics:cbState['cb-lyrics'],genre:cbState['cb-genre'],
    replaygain:cbState['cb-rg'],dr:cbState['cb-dr'],
    year:cbState['cb-year'],overwrite:cbState['cb-overwrite'],
  };
  window.pywebview.api.run_tagger(opts);
}
function stopTagger(){
  window.pywebview.api.stop_tagger();
  document.getElementById('stopBtn').disabled=true;
  document.getElementById('stopBtn').textContent='⏹ Stopping...';
}

// Called from Python
function onLog(msg,type){appendLog(msg,type)}
function onProgress(pct){document.getElementById('taggerProgress').style.width=Math.round(pct*100)+'%'}
function onStatus(msg,isErr){setStatus(msg,isErr||false)}
function onRowUpdate(path,updates){updateTaggerRow(path,updates)}
function onTaggerDone(){
  document.getElementById('runBtn').disabled=false;
  document.getElementById('stopBtn').disabled=true;
  document.getElementById('stopBtn').textContent='⏹ Stop';
  document.getElementById('taggerProgress').style.width='100%';
  setStatus('Done');
}

// ─── CHECKER TABLE ───────────────────────────────────────
let checkerData=[];
let checkerSortDir={};
const VERDICT_TIPS={
  hires:'Source is lossless and spectrum analysis confirms real content above 22 kHz — genuine Hi-Res recording.',
  cd:'16bit/44.1kHz container. Standard CD quality — no upscaling detected.',
  upscale:'Lossless source but very little energy above 22 kHz — likely upscaled from a CD master.',
  suspicious:'Low high-frequency content (below 16 kHz). Possible lossy upscale or heavily compressed master.',
  fake:'FLAD model detected lossy codec artifacts (MP3/AAC/Opus) in the spectrum — fake lossless.',
  error:'Analysis could not be completed for this file.',
};
const VERDICT_MAP={
  hires:['b-ok','Genuine Hi-Res'],cd:['b-cd','Proper CD'],
  upscale:['b-warn','CD upscale'],suspicious:['b-susp','Low HF content'],
  fake:['b-err','Fake lossless'],error:['','Error'],
};

function onCheckerRow(row){
  checkerData.push(row);
  renderCheckerTable();
}
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
    const badge=cls
      ?`<span class="badge ${cls}" data-tip="${escHtml(tip)}">${escHtml(label)}</span>`
      :escHtml(label);
    return `<tr>
      <td title="${escHtml(r.file)}">${escHtml(r.file)}</td>
      <td>${escHtml(r.source)}</td>
      <td>${escHtml(r.container)}</td>
      <td style="text-align:center;font-family:var(--mono)">${escHtml(r.dr)}</td>
      <td>${badge}</td>
    </tr>`;
  }).join('');
}

function sortChecker(col){
  const keys=['file','source','container','dr','verdict'];
  const key=keys[col];
  checkerSortDir[key]=!checkerSortDir[key];
  checkerData.sort((a,b)=>{
    const av=String(a[key]||'');const bv=String(b[key]||'');
    return checkerSortDir[key]?av.localeCompare(bv):bv.localeCompare(av);
  });
  renderCheckerTable();
}

function runChecker(){
  const path=document.getElementById('checker-path').value.trim();
  if(!path)return;
  document.getElementById('checkerBody').innerHTML='';
  document.getElementById('checkerSummary').textContent='Scanning...';
  document.getElementById('scanBtn').disabled=true;
  document.getElementById('checkerProgress').style.width='0%';
  checkerData=[];
  window.pywebview.api.run_checker(path);
}

let checkerLogPath=null;
function openCheckerLog(){
  window.pywebview.api.open_log();
}

// ─── SETTINGS ────────────────────────────────────────────
function saveSettings(){
  window.pywebview.api.save_settings({
    genius_token:document.getElementById('genius-token').value.trim(),
    lastfm_key:document.getElementById('lastfm-key').value.trim(),
    log_path:document.getElementById('log-path').value.trim(),
  }).then(()=>{appendLog('✓ Settings saved','ok');setStatus('Settings saved')});
}

function loadSettings(cfg){
  if(cfg.GENIUS_TOKEN&&cfg.GENIUS_TOKEN!=='YOUR_GENIUS_API_TOKEN')
    document.getElementById('genius-token').value=cfg.GENIUS_TOKEN;
  if(cfg.LASTFM_KEY&&cfg.LASTFM_KEY!=='YOUR_LASTFM_API_KEY')
    document.getElementById('lastfm-key').value=cfg.LASTFM_KEY;
  if(cfg.LOG_PATH) document.getElementById('log-path').value=cfg.LOG_PATH;
  if(cfg.MUSIC_ROOT){
    document.getElementById('music-path').value=cfg.MUSIC_ROOT;
    document.getElementById('checker-path').value=cfg.MUSIC_ROOT;
  }
}

// ─── UTILS ───────────────────────────────────────────────
function escHtml(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── FLOATING TOOLTIP ────────────────────────────────────
const floatTip = document.createElement('div');
floatTip.id = 'floatTip';
document.body.appendChild(floatTip);

document.addEventListener('mouseover', e => {
  const badge = e.target.closest('.badge[data-tip]');
  if (badge) {
    floatTip.textContent = badge.dataset.tip;
    floatTip.style.display = 'block';
  }
});
document.addEventListener('mousemove', e => {
  if (floatTip.style.display === 'block') {
    const x = e.clientX + 12;
    const y = e.clientY - floatTip.offsetHeight - 8;
    floatTip.style.left = Math.min(x, window.innerWidth - 300) + 'px';
    floatTip.style.top = Math.max(y, 4) + 'px';
  }
});
document.addEventListener('mouseout', e => {
  if (e.target.closest('.badge[data-tip]')) {
    floatTip.style.display = 'none';
  }
});

// Init
window.addEventListener('pywebviewready',()=>{
  window.pywebview.api.get_config().then(cfg=>{
    loadSettings(cfg);
    if(cfg.MUSIC_ROOT) loadFileList();
  });
});
</script>
</body>
</html>
"""


# ============================================================
# PYTHON API
# ============================================================

class Api:
    def __init__(self, window_ref):
        self._win = window_ref
        self._stop = False
        self._log_path = None

    def _js(self, fn, *args):
        arg_str = ', '.join(json.dumps(a) for a in args)
        self._win.evaluate_js(f'{fn}({arg_str})')

    def get_config(self):
        return load_config()

    def browse_folder(self):
        result = self._win.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
        return result[0] if result else None

    def close_app(self):      self._win.destroy()
    def minimize_app(self):   self._win.minimize()
    def toggle_maximize(self):
        if self._win.maximized: self._win.restore()
        else: self._win.maximize()

    def save_settings(self, data: dict):
        cfg = load_config()
        if data.get('genius_token'): cfg['GENIUS_TOKEN'] = data['genius_token']
        if data.get('lastfm_key'):   cfg['LASTFM_KEY']   = data['lastfm_key']
        cfg['LOG_PATH'] = data.get('log_path', '')
        save_config(cfg)

    def stop_tagger(self):
        self._stop = True

    def get_file_list(self, path: str):
        """Returns list of file info dicts including existing RG/DR/lyrics/year/genre."""
        files = scan_audio_files(path)
        result = []
        for fp in files:
            ext = fp.lower().rsplit('.', 1)[-1]
            year_raw = read_year(fp) or ''
            year_val = fix_year(year_raw) if year_raw else '—'
            has_l = has_lyrics(fp)

            # Read genre
            genre_val = ''
            try:
                if ext == 'mp3':
                    a = MP3(fp, ID3=ID3)
                    tcon = (a.tags or {}).get('TCON')
                    genre_val = str(tcon.text[0]) if tcon else ''
                elif ext == 'flac':
                    genre_val = FLAC(fp).get('genre', [''])[0]
                elif ext == 'm4a':
                    v = (MP4(fp).tags or {}).get('\xa9gen')
                    genre_val = v[0] if v else ''
                elif ext == 'ogg':
                    genre_val = OggVorbis(fp).get('genre', [''])[0]
            except Exception:
                pass

            # Read existing DR tag
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
            except Exception:
                pass

            # Read existing ReplayGain tag
            rg_val = '—'
            try:
                if ext == 'mp3':
                    t = EasyID3(fp)
                    v = t.get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
                elif ext == 'flac':
                    v = FLAC(fp).get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
                elif ext == 'ogg':
                    v = OggVorbis(fp).get('replaygain_track_gain', [None])[0]
                    if v: rg_val = v
            except Exception:
                pass

            result.append({
                'path':   fp,
                'file':   os.path.basename(fp),
                'year':   year_val,
                'lyrics': has_l,
                'genre':  genre_val,
                'dr':     dr_val,
                'rg':     rg_val,
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

                status('Scanning folder...')
                log(f'Scanning {music_root}...', 'info')
                files = scan_audio_files(music_root)
                total = len(files)
                log(f'Found {total} files', 'ok')

                genius_client = None
                if opts.get('lyrics'):
                    import lyricsgenius as lg
                    genius_client = lg.Genius(genius_token)
                    genius_client.verbose = False
                    genius_client.remove_section_headers = False

                # ReplayGain — run first on entire folder
                if opts.get('replaygain'):
                    status('Applying ReplayGain...')
                    log('Applying ReplayGain...', 'info')
                    res = subprocess.run(
                        ['/opt/homebrew/bin/rsgain', 'easy', music_root],
                        capture_output=True, text=True
                    )
                    if res.returncode == 0:
                        log('✓ ReplayGain done', 'ok')
                        # Parse per-track gain from rsgain output
                        current_file = None
                        rg_map = {}
                        for line in res.stdout.splitlines():
                            line = line.strip()
                            if line.startswith('Track:'):
                                current_file = line.replace('Track:', '').strip()
                            elif 'Gain:' in line and current_file:
                                gain = line.split('Gain:')[-1].strip().split()[0]
                                rg_map[current_file] = gain
                                log(f'  RG {os.path.basename(current_file)}: {gain} dB', 'dim')
                        # Update table rows
                        for fp, gain in rg_map.items():
                            row(fp, {'rg': f'{gain} dB'})
                    else:
                        log(f'✗ rsgain error: {res.stderr[:100]}', 'err')
                        status('ReplayGain failed', True)

                retry_files = []

                for idx, filepath in enumerate(files):
                    if self._stop:
                        log('⛔ Stopped by user', 'err')
                        status('Stopped')
                        break

                    fname = os.path.basename(filepath)
                    title, artist = get_tags(filepath)
                    pct = (idx + 1) / total
                    self._js('onProgress', pct)
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
                        dr = calculate_dr14(filepath)
                        if dr:
                            write_dr_tag(filepath, dr)
                            updates['dr'] = f'DR{dr["dr"]}'
                            updates['rg'] = f'{dr["peak_db"]:.1f}dB'
                            log(f'🎛 {fname}: DR{dr["dr"]}')
                        else:
                            updates['dr'] = '—'

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
                                    log(f'🔍 {fname}: retrying without parentheses...', 'dim')
                                    song = genius_client.search_song(title_clean, artist)
                            except Exception as e:
                                err = str(e)
                                if 'reset' in err.lower() or 'timed out' in err.lower():
                                    retry_files.append(filepath)
                                    log(f'💥 {fname}: network error — will retry', 'err')
                                    status(f'Network error: {fname}', True)
                                else:
                                    log(f'💥 {fname}: {err[:60]}', 'err')
                                    status(f'Genius error: {err[:40]}', True)
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
                                    genre = get_genre_lastfm(artist, title, lastfm_key)
                                    if not genre and song:
                                        try:
                                            tag = song.to_dict().get('primary_tag', {})
                                            genre = (tag.get('name','') or '').title() or None
                                        except Exception:
                                            pass
                                    if genre:
                                        write_genre(filepath, genre)
                                        log(f'🎸 {fname}: genre → {genre}', 'ok')
                                        updates['genre'] = genre
                                    else:
                                        status(f'Genre not found: {artist}', True)

                    if had_error:
                        updates['status'] = 'err'
                    row(filepath, updates)

                # Retry
                if retry_files and not self._stop:
                    log(f'\n🔁 Retrying {len(retry_files)} files...', 'info')
                    status('Retrying failed files...')
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
                            except Exception:
                                log(f'✗ {fname}: retry failed', 'err')
                                row(filepath, {'status': 'err'})

                log('\n✅ Done!', 'ok')
                status('Done')
            except Exception as e:
                self._js('onLog', f'❌ Fatal error: {e}', 'err')
                self._js('onStatus', f'Fatal error: {e}', True)
            finally:
                self._js('onTaggerDone')

        threading.Thread(target=worker, daemon=True).start()

    def run_checker(self, path: str):
        def worker():
            try:
                files = [f for f in scan_audio_files(path) if f.lower().endswith('.flac')]
                total = len(files)
                if total == 0:
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

    def open_log(self):
        if self._log_path and os.path.exists(self._log_path):
            subprocess.run(['open', self._log_path])
        else:
            self._js('onLog', 'No log file yet — run a scan first', 'err')


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
    window.expose(api.get_config)
    window.expose(api.browse_folder)
    window.expose(api.close_app)
    window.expose(api.minimize_app)
    window.expose(api.toggle_maximize)
    window.expose(api.save_settings)
    window.expose(api.stop_tagger)
    window.expose(api.get_file_list)
    window.expose(api.run_tagger)
    window.expose(api.run_checker)
    window.expose(api.open_log)

    webview.start(debug=False)
