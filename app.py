"""
KhmerDub — CustomTkinter Native GUI
Supports PyInstaller standalone executable.

TTS Strategy: segment-level timed dubbing
  - Generate TTS audio per subtitle segment
  - Speed-adjust each clip to fit within its time slot
  - Overlay clips at exact timestamps onto a silent track
  - Merge timed audio with original video via FFmpeg
"""
import os
import sys
import uuid
import threading
import subprocess
import time
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import customtkinter as ctk
import json
from tkinter import filedialog, messagebox

import yt_dlp
import whisper
from deep_translator import GoogleTranslator
import edge_tts
import argparse
import librosa
import numpy as np
from pydub import AudioSegment

def get_bin_path(bin_name):
    # Determine the directory of the executable or script
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    
    # Check if bundled in ffmpeg_bin (PyInstaller)
    bundled_path = os.path.join(base_dir, 'ffmpeg_bin', bin_name)
    if os.path.exists(bundled_path):
        return bundled_path
        
    local_path = os.path.join(base_dir, bin_name)
    if os.path.exists(local_path):
        return local_path
    return bin_name

FFMPEG_CMD = get_bin_path('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
FFPROBE_CMD = get_bin_path('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
import cv2
from PIL import Image
from google import genai

# ── Runtime directories ───────────
_BASE      = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'KhmerDub_Output')
TEMP_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'KhmerDub', 'temp')
API_KEYS_PATH = os.path.join(TEMP_DIR, 'api_keys.json')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Fix stdout for console=False .exe builds and encoding issues
# In PyInstaller with console=False, stdout can be None (crashes on print)
# On Windows consoles, cp1252 encoding can't handle emoji (crashes on print)
import io
if sys.stdout is None or not hasattr(sys.stdout, 'reconfigure'):
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', errors='replace')
else:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', errors='replace')
if sys.stderr is None or not hasattr(sys.stderr, 'reconfigure'):
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', errors='replace')
else:
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', errors='replace')

def debug_log(msg):
    log_file = os.path.join(TEMP_DIR, 'KhmerDub_debug.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            from datetime import datetime
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except: pass
    try:
        print(msg)
    except: pass


# ── Bundle FFmpeg into PATH ──────────────────────────────────
# Add the base directory (sys._MEIPASS) to PATH so that third-party libs like whisper
# can find the bundled ffmpeg.exe when they call subprocess.run(['ffmpeg', ...])
os.environ['PATH'] = _BASE + os.pathsep + os.environ.get('PATH', '')

_whisper_model = None
app_gui = None  # Global reference to GUI


def get_whisper_model(speed_setting="High Quality (Slow)"):
    global _whisper_model
    target_model_name = "small" if speed_setting == "High Quality (Slow)" else "base"
    
    # If a model is already loaded and it's the right one, return it
    if _whisper_model is not None and getattr(_whisper_model, '_loaded_model_name', None) == target_model_name:
        return _whisper_model
        
    print(f'[KhmerDub] Loading Whisper model ({target_model_name})…')
    _whisper_model = whisper.load_model(target_model_name)
    _whisper_model._loaded_model_name = target_model_name
    print('[KhmerDub] Whisper model ready.')
    return _whisper_model


# ─────────────────────────────────────────────────────────────
#  LANGUAGE CONFIGURATION
# ─────────────────────────────────────────────────────────────
LANGUAGE_CONFIG = {
    'Khmer': {
        'trans_code': 'km',
        'voice_male': 'km-KH-PisethNeural',
        'voice_female': 'km-KH-SreymomNeural',
        'label': 'Khmer (ខ្មែរ)',
        'font': 'Battambang'
    },
    'English': {
        'trans_code': 'en',
        'voice_male': 'en-US-ChristopherNeural',
        'voice_female': 'en-US-JennyNeural',
        'label': 'English',
        'font': 'Arial'
    },
    'Chinese': {
        'trans_code': 'zh-CN',
        'voice_male': 'zh-CN-YunxiNeural',
        'voice_female': 'zh-CN-XiaoxiaoNeural',
        'label': 'Chinese (中文)',
        'font': 'Microsoft YaHei'
    }
}

# ─────────────────────────────────────────────────────────────
#  GENDER DETECTION  (librosa pitch analysis)
# ─────────────────────────────────────────────────────────────
def _analyse_pitch(y, sr) -> str:
    """
    Male: median F0 < 160 Hz  |  Female: median F0 >= 160 Hz
    """
    try:
        f0, voiced_flag, _ = librosa.pyin(
            y,
            fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C6'),
            sr=sr
        )
        voiced_f0 = f0[voiced_flag & ~np.isnan(f0)]
        if len(voiced_f0) == 0:
            return 'female'
        return 'male' if float(np.median(voiced_f0)) < 160.0 else 'female'
    except Exception:
        return 'female'

def detect_voice_gender(audio_path: str) -> str:
    """Detect gender for entire audio file (used for UI badge)."""
    try:
        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=60)
        return _analyse_pitch(y, sr)
    except Exception as exc:
        print(f'[gender/full] {exc}')
        return 'female'

def detect_segment_gender(audio_path: str, start_sec: float, end_sec: float, video_path: str = None, api_key: str = None) -> str:
    """Detect gender for a specific time slice using Gemini Vision if available, fallback to audio pitch."""
    # 1. Smart Face Tracker: Gemini Vision
    if video_path and api_key and 'cv2' in globals():
        try:
            cap = cv2.VideoCapture(video_path)
            mid_sec = (start_sec + end_sec) / 2.0
            cap.set(cv2.CAP_PROP_POS_MSEC, mid_sec * 1000)
            ret, frame = cap.read()
            cap.release()
            
            if ret:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                    "Identify the gender of the primary person speaking or the most prominent character in this image. Reply with exactly one word: 'male' or 'female'. If you cannot tell, reply 'unknown'.",
                    img
                ])
                ans = response.text.strip().lower()
                if 'male' in ans and 'female' not in ans:
                    print(f'[Face Tracker {start_sec:.1f}s] Detected: male')
                    return 'male'
                elif 'female' in ans:
                    print(f'[Face Tracker {start_sec:.1f}s] Detected: female')
                    return 'female'
                else:
                    print(f'[Face Tracker {start_sec:.1f}s] Unknown, falling back to audio...')
        except Exception as e:
            print(f'[Face Tracker Error] {e}')

    # 2. Fallback to Audio Pitch Analysis
    duration = max(end_sec - start_sec, 0.3)
    try:
        y, sr = librosa.load(
            audio_path, sr=22050, mono=True,
            offset=start_sec, duration=duration
        )
        if len(y) < 512:
            return 'female'
        return _analyse_pitch(y, sr)
    except Exception as exc:
        print(f'[gender/seg {start_sec:.1f}s] {exc}')
        return 'female'

def gender_to_label(gender: str) -> str:
    return '🎙️ Male' if gender == 'male' else '🎙️ Female'


# ─────────────────────────────────────────────────────────────
#  SPEAKER CLUSTERING — assign unique voice per character
# ─────────────────────────────────────────────────────────────
# Available KiriTTS voices per gender
KIRI_MALE_VOICES   = ['Chanda', 'Bora', 'Arun', 'Oudom', 'Rithy', 'Setha', 'Rithy Seang']
KIRI_FEMALE_VOICES = ['Maly', 'Neary', 'Phanin', 'Theary']

def cluster_speakers(audio_path: str, segments: list, genders: list, voice_male: str = 'Rithy', voice_female: str = 'Maly') -> list:
    """
    Fingerprint each segment with MFCC features, then cluster
    into N unique speakers. Returns a list of KiriTTS voice IDs,
    one per segment. Each unique speaker always gets the same voice.
    """
    try:
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.preprocessing import StandardScaler

        features = []
        valid_idx = []

        for i, seg in enumerate(segments):
            text = seg['text'].strip()
            if not text:
                features.append(None)
                continue
            try:
                y, sr = librosa.load(
                    audio_path, sr=22050, mono=True,
                    offset=seg['start'],
                    duration=max(seg['end'] - seg['start'], 0.3)
                )
                mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
                feat = np.concatenate([mfcc.mean(axis=1), mfcc.std(axis=1)])
                features.append(feat)
                valid_idx.append(i)
            except Exception:
                features.append(None)

        if len(valid_idx) < 2:
            # Not enough data — fall back to simple gender mapping
            return _fallback_voices(genders, voice_male, voice_female)

        X = np.array([features[i] for i in valid_idx])
        X = StandardScaler().fit_transform(X)

        # Auto-select number of clusters (2–6 speakers max)
        n_clusters = min(max(2, len(valid_idx) // 5), 6)
        labels = AgglomerativeClustering(n_clusters=n_clusters).fit_predict(X)

        # Map cluster → majority gender
        cluster_genders = {}
        for pos, seg_i in enumerate(valid_idx):
            cluster = labels[pos]
            gender = genders[seg_i] or 'male'
            if cluster not in cluster_genders:
                cluster_genders[cluster] = {'male': 0, 'female': 0}
            cluster_genders[cluster][gender] += 1
            
        cluster_voice_map = {}
        import random
        used_males = set()
        used_females = set()

        for cluster, counts in cluster_genders.items():
            majority_gender = 'male' if counts['male'] >= counts['female'] else 'female'
            if majority_gender == 'male':
                if voice_male == "Auto Detect":
                    available = [v for v in KIRI_MALE_VOICES if v not in used_males]
                    if not available:
                        available = KIRI_MALE_VOICES # reset if exhausted
                    chosen = random.choice(available) if available else "Rithy"
                    used_males.add(chosen)
                    cluster_voice_map[cluster] = chosen
                else:
                    cluster_voice_map[cluster] = voice_male
            else:
                if voice_female == "Auto Detect":
                    available = [v for v in KIRI_FEMALE_VOICES if v not in used_females]
                    if not available:
                        available = KIRI_FEMALE_VOICES
                    chosen = random.choice(available) if available else "Maly"
                    used_females.add(chosen)
                    cluster_voice_map[cluster] = chosen
                else:
                    cluster_voice_map[cluster] = voice_female

        # Build final voice list aligned to all segments
        voices = []
        for i, seg in enumerate(segments):
            if i in valid_idx:
                pos = valid_idx.index(i)
                fallback_voice = cluster_voice_map[labels[pos]] # we know it exists from the map above
                voices.append(cluster_voice_map.get(labels[pos], fallback_voice))
            else:
                # If segment was unclusterable but has a gender
                is_male = (genders[i] or 'male') == 'male'
                if is_male:
                    fallback_voice = random.choice(KIRI_MALE_VOICES) if (voice_male == "Auto Detect" and KIRI_MALE_VOICES) else voice_male
                else:
                    fallback_voice = random.choice(KIRI_FEMALE_VOICES) if (voice_female == "Auto Detect" and KIRI_FEMALE_VOICES) else voice_female
                voices.append(fallback_voice)

        print(f"[Speaker Cluster] Detected {n_clusters} unique characters → voices: {cluster_voice_map}")
        return voices

    except Exception as e:
        print(f"[Speaker Cluster Error] {e} — using fallback")
        return _fallback_voices(genders, voice_male, voice_female)


def _fallback_voices(genders: list, voice_male: str = 'Rithy', voice_female: str = 'Maly') -> list:
    """Fallback if clustering fails: directly map male/female."""
    import random
    if voice_male == "Auto Detect":
        voice_male = random.choice(KIRI_MALE_VOICES) if KIRI_MALE_VOICES else 'Rithy'
    if voice_female == "Auto Detect":
        voice_female = random.choice(KIRI_FEMALE_VOICES) if KIRI_FEMALE_VOICES else 'Maly'
    voices = []
    for g in genders:
        voices.append(voice_male if (g or 'male') == 'male' else voice_female)
    return voices


# ─────────────────────────────────────────────────────────────
#  VIDEO DURATION
# ─────────────────────────────────────────────────────────────
def get_video_duration(video_path: str) -> float:
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        [FFPROBE_CMD, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def parse_subtitle_file(filepath):
    """Parse a VTT or SRT file into a list of segments with exact timing."""
    segments = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        import re
        # Normalize newlines
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        
        # Split into blocks (separated by multiple newlines)
        blocks = re.split(r'\n{2,}', content.strip())
        
        for block in blocks:
            lines = block.strip().split('\n')
            if not lines:
                continue
            
            # Find the line with the timestamp
            ts_line = -1
            for i, line in enumerate(lines):
                if '-->' in line:
                    ts_line = i
                    break
                    
            if ts_line == -1:
                continue
                
            ts_match = re.search(r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})', lines[ts_line])
            if not ts_match:
                continue
                
            def time_to_sec(t_str):
                t_str = t_str.replace(',', '.')
                h, m, s = t_str.split(':')
                return int(h) * 3600 + int(m) * 60 + float(s)
                
            start = time_to_sec(ts_match.group(1))
            end = time_to_sec(ts_match.group(2))
            
            # Text is everything after the timestamp line
            text_lines = lines[ts_line+1:]
            text = ' '.join(text_lines)
            
            # Clean text (remove VTT tags, HTML tags)
            text = re.sub(r'<[^>]+>', '', text).strip()
            
            if text and not text.isdigit():
                segments.append({'start': start, 'end': end, 'text': text})
                
        return segments
    except Exception as e:
        print(f'[Subtitle Parse Error] {e}')
        return []

# ─────────────────────────────────────────────────────────────
#  AUDIO SPEED ADJUST  (pydub frame-rate trick)
# ─────────────────────────────────────────────────────────────
def speed_change(sound: AudioSegment, speed: float) -> AudioSegment:
    """
    Change playback speed WITHOUT pitch shift using ffmpeg atempo filter.
    """
    if abs(speed - 1.0) < 0.01:
        return sound
        
    import tempfile
    import subprocess
    import os
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in:
        in_path = f_in.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
        out_path = f_out.name
        
    try:
        sound.export(in_path, format="wav")
        # Run ffmpeg with atempo to stretch/compress time while preserving pitch
        subprocess.run([
            FFMPEG_CMD, '-y', '-i', in_path, 
            '-filter:a', f'atempo={speed}', 
            out_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        altered = AudioSegment.from_file(out_path, format="wav")
        return altered
    except Exception as e:
        print(f"Error in speed_change: {e}")
        return sound
    finally:
        try: os.remove(in_path)
        except: pass
        try: os.remove(out_path)
        except: pass


# ─────────────────────────────────────────────────────────────
#  SEGMENT TTS  — generate audio for one subtitle segment
# ─────────────────────────────────────────────────────────────
async def _generate_segment_tts_async(text: str, voice_id: str, out_path: str, options: dict = None, retries=4):
    options = options or {}
    speed_str = options.get('voice_speed', '1.0x').replace('x', '')
    try:
        speed_float = float(speed_str)
    except ValueError:
        speed_float = 1.0

    # Calculate Edge-TTS rate (e.g. 1.5 -> +50%, 0.9 -> -10%)
    rate_pct = int((speed_float - 1.0) * 100)
    rate_sign = "+" if rate_pct >= 0 else ""
    rate_str = f"{rate_sign}{rate_pct}%"

    for attempt in range(retries):
        try:
            comm = edge_tts.Communicate(text, voice_id, rate=rate_str)
            await comm.save(out_path)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise e
            await asyncio.sleep(2 ** attempt)

def generate_segment_tts(text: str, voice_id: str, out_path: str, options: dict = None):
    options = options or {}
    engine = options.get('tts_engine', 'Edge-TTS')
    api_key = options.get('kiritts_key', '').strip()
    
    if engine == 'KiriTTS' and api_key:
        import requests
        # Strip display suffix added for clone voices in the UI
        clean_voice_id = voice_id.replace(' (Clone)', '').strip()
        # Build list of known KiriTTS voices (without display suffixes)
        all_kiri_clean = [v.replace(' (Clone)', '') for v in KIRI_MALE_VOICES + KIRI_FEMALE_VOICES]

        if clean_voice_id in all_kiri_clean and clean_voice_id != 'Auto Detect':
            kiri_voice = clean_voice_id  # Already a valid KiriTTS voice name
        else:
            # Map Edge-TTS voice ID → KiriTTS gender default
            is_male_edge = any(k in voice_id for k in ('Piseth', 'Christopher', 'Yunxi', 'Guy'))
            # Better default: use Rithy Seang (the user's clone) for male, Maly for female
            kiri_voice = 'Rithy Seang' if is_male_edge or voice_id in ('Auto Detect', '') else 'Maly'
        
        try:
            url = 'https://api.kiritts.com/v1/audio/speech'
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            data = {
                'model': 'tts-1',
                'input': text,
                'voice': kiri_voice
            }
            res = requests.post(url, headers=headers, json=data, timeout=15)
            if res.status_code == 200:
                with open(out_path, 'wb') as f:
                    f.write(res.content)
                
                # KiriTTS does not support speed change; keep original speed to avoid artifacts.
                # (speed adjustment disabled for KiriTTS)
                # No speed processing performed here.

                return  # Success!
            else:
                debug_log(f"[KiriTTS Error] Voice: {kiri_voice}, Code: {res.status_code}, Res: {res.text}. Falling back to Edge-TTS...")
        except Exception as e:
            debug_log(f"[KiriTTS Request Failed] Voice: {kiri_voice}, Error: {e}. Falling back to Edge-TTS...")

    # Default/Fallback: Edge-TTS
    if voice_id in KIRI_MALE_VOICES or voice_id in KIRI_FEMALE_VOICES:
        target_lang = options.get('target_lang', 'Khmer') if options else 'Khmer'
        lang_cfg = LANGUAGE_CONFIG.get(target_lang, LANGUAGE_CONFIG['Khmer'])
        voice_id = lang_cfg['voice_male'] if voice_id in KIRI_MALE_VOICES else lang_cfg['voice_female']

    asyncio.run(_generate_segment_tts_async(text, voice_id, out_path, options=options))

# ─────────────────────────────────────────────────────────────
#  BUILD TIMED DUB TRACK  (per-segment gender detection)
# ─────────────────────────────────────────────────────────────
def build_timed_audio(
    segments: list,
    bg_audio_path: str,
    video_duration: float,
    job_id: str,
    progress_callback=None,
    voice_male: str = 'km-KH-PisethNeural',
    voice_female: str = 'km-KH-SreymomNeural',
    options: dict = None,
    original_audio_path: str = None,
    genders: list = None,
    kiri_voices: list = None,
    video_path: str = None,
    sequential_mode: bool = False,
    is_vocals_removed: bool = False
) -> str:
    """
    Fast parallel version:
      1. Pre-detect ALL segment genders in parallel
      2. Generate ALL TTS clips in parallel (ThreadPoolExecutor)
      3. Overlay clips at exact timestamps
    """
    try:
        master = AudioSegment.from_file(audio_path)
        target_dur_ms = int(video_duration * 1000) + 10000
        if len(master) < target_dur_ms:
            master = master + AudioSegment.silent(duration=target_dur_ms - len(master))
    except Exception as e:
        print(f"[Audio Load Error] {e}")
        master = AudioSegment.silent(duration=int(video_duration * 1000) + 10000)

    n = len(segments)

    # ── Step 1: Detect ALL genders in parallel ────────────────
    def detect_gender_for_seg(seg):
        api_key = options.get('translator_key') if options and 'Gemini' in options.get('translator', '') else (options.get('transcriber_key') if options else None)
        return detect_segment_gender(original_audio_path or audio_path, seg['start'], seg['end'], video_path=video_path, api_key=api_key)

    forced_gender = None
    if options:
        dub_gender_opt = options.get('dub_gender', 'Auto Detect')
        if dub_gender_opt == 'Male Only':
            forced_gender = 'male'
        elif dub_gender_opt == 'Female Only':
            forced_gender = 'female'

    if forced_gender:
        # User chose a fixed gender — skip auto-detection entirely
        genders = [forced_gender] * n
        print(f'[Gender] Forced to {forced_gender} for all {n} segments (no auto-detect)')
    elif genders is None:
        genders = [None] * n
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(detect_gender_for_seg, seg): i for i, seg in enumerate(segments) if seg['text'].strip()}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    genders[i] = fut.result()
                except Exception:
                    genders[i] = 'male'

    for i, seg in enumerate(segments):
        seg['gender'] = genders[i] or 'unknown'

    # ── Step 1.5: Speaker clustering (KiriTTS only) ───────────
    # We cluster the characters and map each character's majority gender to voice_male/voice_female
    if kiri_voices is None:
        if options and options.get('tts_engine') == 'KiriTTS' and options.get('kiritts_key', '').strip():
            print('[Speaker Cluster] Running speaker clustering for KiriTTS to track characters...')
            kiri_voices = cluster_speakers(original_audio_path or audio_path, segments, genders, voice_male, voice_female)

    # ── Step 2: Generate ALL TTS clips in parallel ────────────
    tts_paths = [None] * n

    def generate_tts_for_seg(i, seg):
        text = seg['text'].strip()
        if not text:
            return i, None
        gender = genders[i] or 'male'
        # KiriTTS: use clustered voice; Edge-TTS: use gender-based voice
        if gender == 'narrator':
            voice_id = voice_male  # Use male voice for narrator
        elif kiri_voices is not None:
            voice_id = kiri_voices[i]  # KiriTTS voice name e.g. 'Chanda'
        else:
            voice_id = voice_male if gender == 'male' else voice_female
        out_path = os.path.join(TEMP_DIR, f'{job_id}_seg{i}.mp3')
        generate_segment_tts(text, voice_id, out_path, options=options)
        return i, out_path

    done_count = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(generate_tts_for_seg, i, seg): i for i, seg in enumerate(segments)}
        for fut in as_completed(futures):
            try:
                i, path = fut.result()
                tts_paths[i] = path
            except Exception as e:
                print(f"[TTS parallel error] {e}")
            done_count += 1
            if progress_callback:
                gender_label = genders[futures[fut]] or 'male'
                progress_callback(int(done_count / n * 100), done_count, n, gender_label)

    # ── Step 3: Overlay clips in order ───────────────────────
    bg_layer = master
    speech_layer = AudioSegment.silent(duration=len(master))

    for i, seg in enumerate(segments):
        path = tts_paths[i]
        if not path or not os.path.exists(path):
            continue
        try:
            clip = AudioSegment.from_file(path)
            text = seg['text'].strip()

            # Apply speed for KiriTTS using the user's chosen speed dropdown value
            engine = options.get('tts_engine', 'Edge-TTS') if options else 'Edge-TTS'
            if engine == 'KiriTTS':
                speed_str = options.get('voice_speed', '1.0x').replace('x', '') if options else '1.0'
                try:
                    speed_float = float(speed_str)
                except ValueError:
                    speed_float = 1.0
                if speed_float != 1.0:
                    clip = speed_change(clip, speed_float)

            if options and options.get('smart_emotion'):
                if text.endswith('!') or text.endswith('?!'):
                    clip = clip + 3
                    clip = speed_change(clip, 1.15)
                elif text.endswith('...') or text.endswith('…'):
                    clip = clip - 2
                    clip = speed_change(clip, 0.85)
                    
            if seg.get('gender') == 'narrator':
                # Slightly speed up narrator and increase volume so they sound distinct
                clip = speed_change(clip, 1.15)
                clip = clip + 2

            if options and options.get('auto_sync', True) and not sequential_mode and seg.get('end', 0) > 0:
                target_len = int((seg['end'] - seg['start']) * 1000)
                if target_len > 0:
                    current_len = len(clip)
                    speed_factor = current_len / target_len
                    # Only speed up if it's too long, NEVER slow down (to avoid drunk voices)
                    # Clamp speed up to 1.5x max to avoid chipmunk voices.
                    speed_factor = max(1.0, min(speed_factor, 1.5))
                    if abs(speed_factor - 1.0) > 0.05:
                        clip = speed_change(clip, speed_factor)

            clip_len  = len(clip)
            if sequential_mode:
                if i == 0:
                    curr_time = 0
                start_ms = curr_time
            else:
                start_ms = int(seg['start'] * 1000)
                
            end_ms = start_ms + clip_len
            if sequential_mode:
                curr_time = end_ms + 800  # 800ms pause between sentences
            
            # Duck the original audio slightly during dubbed segments so the voice stands out
            # If vocals were removed, we keep the music and effects at full volume!
            if not is_vocals_removed:
                ducked = bg_layer[start_ms:end_ms] - 6  # Reduce volume by 6dB
                bg_layer = bg_layer[:start_ms] + ducked + bg_layer[end_ms:]
            
            # Overlay speech on the separate speech layer
            speech_layer = speech_layer.overlay(clip, position=start_ms)
            
        except Exception as e:
            print(f'[Overlay seg {i}] Error: {e}')
        finally:
            try: os.remove(path)
            except: pass

    # Combine ducked background with speech layer
    master = bg_layer.overlay(speech_layer)

    out_path = os.path.join(TEMP_DIR, f'{job_id}_timed_dub.wav')
    master.export(out_path, format='wav')
    return out_path


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def upd(job_id: str, **kw):
    debug_log(f"upd called with: {kw}")
    if app_gui:
        # Schedule GUI update on the main thread safely
        app_gui.after(0, app_gui.update_status, kw)
    else:
        debug_log("app_gui is None!")


def seconds_to_srt(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = int(s % 60)
    ms  = int((s % 1) * 1000)
    return f'{h:02d}:{m:02d}:{sec:02d},{ms:03d}'


import textwrap

def write_srt(segments, path: str):
    with open(path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, 1):
            text = seg['text'].strip()
            # Wrap Khmer text every 35 chars so it fits on vertical video screens
            wrapped_text = textwrap.fill(text, width=35, break_long_words=True)
            f.write(
                f"{i}\n"
                f"{seconds_to_srt(seg['start'])} --> {seconds_to_srt(seg['end'])}\n"
                f"{wrapped_text}\n\n"
            )


# ─────────────────────────────────────────────────────────────
#  TRANSLATOR (Robust with retries)
# ─────────────────────────────────────────────────────────────
def robust_translate(text: str, dest='km', retries=5) -> str:
    if not text.strip(): return text
    for attempt in range(retries):
        try:
            translator = GoogleTranslator(source='auto', target=dest)
            res = translator.translate(text)
            if res:
                if "Error 500 (Server Error)" in res or "That's an error" in res:
                    raise Exception(f"Google returned a 500 error page instead of translating: {res[:50]}")
                return res
        except Exception as e:
            if attempt == retries - 1:
                print(f"[Translate Error] Final attempt failed for text '{text[:20]}...': {e}")
                return text
            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s, 8s...
    return text

def robust_translate_batch(texts: list, dest='km', retries=5) -> list:
    """Translate a list of strings in batches to avoid rate limits."""
    if not texts: return []
    # Join with a special separator to give Google Translate context
    separator = " \n\n "
    joined_text = separator.join(texts)
    for attempt in range(retries):
        try:
            translator = GoogleTranslator(source='auto', target=dest)
            res = translator.translate(joined_text)
            if res:
                parts = [p.strip() for p in res.split("\n\n")]
                if len(parts) == len(texts):
                    return parts
                # If split count mismatches, fallback to independent batch translation
                results = translator.translate_batch(texts)
                return [r if r else t for r, t in zip(results, texts)]
        except Exception as e:
            if attempt == retries - 1:
                print(f"[Translate Batch Error] Final attempt failed: {e}")
                return texts
            time.sleep(2 ** attempt)
    return texts

def gemini_translate_batch(texts: list, human_dest='Khmer', dest_code='km', api_key='', video_file=None, retries=3) -> list:
    """Translate using Gemini Pro for perfect conversational context and add speaker tags/visual context."""
    if not texts: return []
    from google import genai
    client = genai.Client(api_key=api_key)
    
    # Combine texts into a single prompt for contextual translation
    text_block = "\n".join(texts)
    prompt = f"""You are an expert movie subtitle translator. 
Translate the following video subtitle script into {human_dest}. 
Maintain conversational tone, context, and gender pronouns.
CRITICAL: Translate accurately and clearly, preserving the full meaning of every word. Do not summarize or drop any details. The translation must be highly detailed and exact.

If a video file is provided, use it to understand who is speaking. For EACH line in the translation, ADD the speaker's identity and any visual context describing what happens. 
For example, output lines exactly like this:
[Boy in truck]: អ្នកមើលទៅស្រស់ស្អាតណាស់។
(The blonde girl smiles, then looks surprised as another car pulls up beside them)

Keep the exact same number of lines as the original script. Do not add any extra markdown formatting outside of the subtitles.

{text_block}
"""
    for attempt in range(retries):
        try:
            # Pass video file if available
            contents = [video_file, prompt] if video_file else [prompt]
            response = client.models.generate_content(model='gemini-2.5-flash', contents=contents)
            
            translated = [line.strip() for line in response.text.strip().split('\n') if line.strip()]
            
            # Fallback if lines don't match (unlikely with pro, but possible)
            if len(translated) == len(texts):
                return translated
            else:
                if attempt == retries - 1:
                    print(f"[Gemini Translate] Line count mismatch (got {len(translated)}, expected {len(texts)}). Falling back to Google.")
                    return robust_translate_batch(texts, dest=dest_code)
                time.sleep(2)
        except Exception as e:
            if attempt == retries - 1:
                print(f"[Gemini Translate Error] Final attempt failed: {e}. Falling back to Google.")
                return robust_translate_batch(texts, dest=dest_code)
            time.sleep(2 ** attempt)
    
def deepseek_translate_batch(texts: list, human_dest='Khmer', dest_code='km', api_key='', retries=3) -> list:
    """Translate using DeepSeek API for accurate conversational context."""
    if not texts: return []
    import requests
    
    text_block = "\n".join(texts)
    prompt = f"""You are an expert movie subtitle translator. 
Translate the following video subtitle script into {human_dest}. 
Maintain conversational tone, context, and gender pronouns.
CRITICAL: Translate accurately and clearly, preserving the full meaning of every word. Do not summarize or drop any details. The translation must be highly detailed and exact.

Keep the exact same number of lines as the original script. Do not add any extra markdown formatting or conversational text, just output the {len(texts)} translated lines.

Script:
{text_block}
"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are a professional translator."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }
    
    for attempt in range(retries):
        try:
            response = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=data, timeout=30)
            response.raise_for_status()
            res_json = response.json()
            raw_text = res_json['choices'][0]['message']['content'].strip()
            
            translated = [line.strip() for line in raw_text.split('\n') if line.strip()]
            
            if len(translated) == len(texts):
                return translated
            else:
                if attempt == retries - 1:
                    print(f"[DeepSeek Translate] Line count mismatch (got {len(translated)}, expected {len(texts)}). Falling back to Google.")
                    return robust_translate_batch(texts, dest=dest_code)
                time.sleep(2)
        except Exception as e:
            if attempt == retries - 1:
                print(f"[DeepSeek Translate Error] Final attempt failed: {e}. Falling back to Google.")
                return robust_translate_batch(texts, dest=dest_code)
            time.sleep(2 ** attempt)
    return texts

def gemini_transcribe_video(video_path: str, api_key: str, human_dest: str, upd_callback=None, job_id=None) -> list:
    """Directly transcribe and translate video to JSON using Gemini 1.5 Pro."""
    from google import genai
    import json
    import time
    client = genai.Client(api_key=api_key)
    
    if upd_callback: upd_callback(job_id, message='☁️ Uploading video directly to Gemini…')
    video_file = client.files.upload(file=video_path)
    
    if upd_callback: upd_callback(job_id, message='⏳ Waiting for Gemini to process video…')
    while video_file.state.name == 'PROCESSING':
        if app_gui and hasattr(app_gui, 'cancel_event') and app_gui.cancel_event.is_set():
            raise Exception("Process stopped by user")
        time.sleep(2)
        video_file = client.files.get(name=video_file.name)
        
    if upd_callback: upd_callback(job_id, message='🧠 Gemini is native-transcribing and translating…')
    
    prompt = f"""You are a professional movie subtitle transcriber and translator.
Watch the provided video carefully. If there are on-screen captions/subtitles in the video, you MUST use the EXACT moment they appear and disappear as your `start` and `end` timestamps. This guarantees perfect lip sync.
CRITICAL INSTRUCTION: You MUST translate all dialogue perfectly into {human_dest}. The "text" field MUST be written in {human_dest}. Do NOT output the original English speech.
Output your response as a RAW JSON array of objects.
Each object must have the following keys:
- "start": exact start time in seconds (float) when the caption appears or actor starts speaking
- "end": exact end time in seconds (float) when the caption disappears or actor stops speaking
- "text": the translated text
- "gender": "male" or "female" based on the speaker's voice
- "speaker_identity": visual context and identity (e.g. "Boy in truck")

Example output:
[
  {{"start": 0.0, "end": 2.5, "text": "អ្នកមើលទៅស្រស់ស្អាតណាស់។", "gender": "male", "speaker_identity": "Boy in truck"}},
  {{"start": 4.0, "end": 5.0, "text": "ឱព្រះជាម្ចាស់អើយ។", "gender": "female", "speaker_identity": "Blonde girl"}}
]
"""
    try:
        from google import genai
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=[video_file, prompt],
            config=genai.types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        raw_text = response.text.strip()
        start_idx = raw_text.find('[')
        end_idx = raw_text.rfind(']')
        
        if start_idx != -1 and end_idx != -1:
            json_str = raw_text[start_idx:end_idx+1]
        else:
            json_str = raw_text
        
        try:
            segments = json.loads(json_str)
        except json.JSONDecodeError:
            # Try to repair common JSON issues
            import re
            repaired = json_str
            # Remove trailing commas before ] or }
            repaired = re.sub(r',\s*\]', ']', repaired)
            repaired = re.sub(r',\s*\}', '}', repaired)
            # If array is truncated (missing closing bracket), try to close it
            if repaired.count('[') > repaired.count(']'):
                # Find last complete object (ending with })
                last_brace = repaired.rfind('}')
                if last_brace != -1:
                    repaired = repaired[:last_brace+1] + ']'
            try:
                segments = json.loads(repaired)
            except json.JSONDecodeError as e2:
                print(f"Failed to parse Gemini JSON even after repair: {e2}")
                print(f"Raw text (first 500 chars): {raw_text[:500]}")
                try: client.files.delete(name=video_file.name)
                except: pass
                raise Exception(f"Gemini returned invalid JSON. Please try again.")
    except Exception as e:
        print(f"Failed to parse Gemini JSON: {e}")
        try: client.files.delete(name=video_file.name)
        except: pass
        raise Exception(f"Gemini API Error: {str(e)}")
        
    try:
        client.files.delete(name=video_file.name)
    except: pass
        
    return segments


# ─────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────
def check_cancel():
    if app_gui and hasattr(app_gui, 'cancel_event') and app_gui.cancel_event.is_set():
        raise Exception("Process stopped by user")

def process_video(job_id: str, video_path: str, options: dict):
    debug_log(f"--- process_video started for job {job_id} ---")
    debug_log(f"video_path: {video_path}")
    debug_log(f"options: {options}")
    audio_path   = os.path.join(TEMP_DIR, f'{job_id}_audio.wav')
    timed_dub    = None
    
    target_lang = options.get('target_lang', 'Khmer')
    lang_cfg = LANGUAGE_CONFIG.get(target_lang, LANGUAGE_CONFIG['Khmer'])
    dest_code = lang_cfg['trans_code']
    voice_male = lang_cfg['voice_male']
    voice_female = lang_cfg['voice_female']
    font_name = lang_cfg['font']
    lang_label = lang_cfg['label']

    try:
        transcriber_engine = options.get('transcriber', 'Whisper Local (Free)')
        transcriber_key = options.get('transcriber_key', '').strip()
        translator_key = options.get('translator_key', '').strip()
        translator_engine = options.get('translator', 'Google Translate (Free)')
        
        # ── 0. Extract audio & Vocal Removal (FOR ALL PIPELINES) ──
        upd(job_id, stage='extract', progress=5, message='🎵 Extracting audio from video…')
        subprocess.run(
            [FFMPEG_CMD, '-i', video_path,
             '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
             audio_path, '-y'],
            check=True, capture_output=True
        )
        bg_audio_path = audio_path
        original_audio_path = audio_path

        if options.get('remove_vocals'):
            upd(job_id, stage='isolate', progress=12, message='🎶 AI Vocal Removal: Isolating background music (this may take a minute)…')
            subprocess.run([
                'demucs', '-n', 'htdemucs', '--two-stems', 'vocals', 
                '-o', TEMP_DIR, '-d', 'cpu', audio_path
            ], check=True)
            
            base_name = os.path.splitext(os.path.basename(audio_path))[0]
            no_vocals_path = os.path.join(TEMP_DIR, 'htdemucs', base_name, 'no_vocals.wav')
            if os.path.exists(no_vocals_path):
                bg_audio_path = no_vocals_path
        
        # ── 0.5 Check for downloaded subtitles ──
        segments = []
        original_subs_found = False
        import glob
        video_base = os.path.splitext(video_path)[0]
        sub_files = [f for f in glob.glob(f"{video_base}*") if f.endswith(('.vtt', '.srt'))]
        if sub_files:
            upd(job_id, stage='transcribe', progress=15, message='📂 Found exact subtitles! Bypassing audio transcription…')
            segments = parse_subtitle_file(sub_files[0])
            if segments:
                original_subs_found = True
                transcriber_engine = "Subtitles" # Force pipeline to skip Gemini Native

        if transcriber_engine == 'Gemini AI (Premium API)' and transcriber_key:
            # ── GEMINI NATIVE PIPELINE ──
            check_cancel()
            upd(job_id, stage='transcribe', progress=10, message='☁️ Initiating Gemini Native Transcription…')
            segments = gemini_transcribe_video(video_path, transcriber_key, lang_label, upd_callback=upd, job_id=job_id)
            if not segments:
                raise Exception("Gemini returned empty segments.")
            
            genders = []
            kiri_voices = []
            translated_segments = []
            
            for seg in segments:
                translated_text = seg.get('text', '')
                
                # Remove English speaker tags so the TTS engine speaks pure Khmer
                final_text = translated_text
                    
                g = seg.get('gender', 'male')
                genders.append(g)
                kiri_voices.append('Chanda' if g == 'male' else 'Neary')
                
                translated_segments.append({
                    'start': float(seg.get('start', 0.0)),
                    'end': float(seg.get('end', 0.0)),
                    'text': final_text
                })
                
            translated_text = " ".join([s['text'] for s in translated_segments])
            if options.get('story_mode'):
                check_cancel()
                upd(job_id, stage='translate', progress=48, message=f'📋 Writing AI Recap in {lang_label}…')
                try:
                    from google import genai
                    import json
                    client = genai.Client(api_key=transcriber_key)
                    
                    # 1-to-1 Recap: Rewrite every single dialogue line to have a storytelling/recap tone, 
                    # but keep the exact same number of segments to preserve PERFECT timing with the video!
                    BATCH_SIZE = 40
                    clean_sentences = []
                    
                    for batch_start in range(0, len(translated_segments), BATCH_SIZE):
                        batch_segments = translated_segments[batch_start:batch_start+BATCH_SIZE]
                        
                        transcript_lines = []
                        for i, seg in enumerate(batch_segments):
                            transcript_lines.append(f"Line {i+1}: {seg['text']}")
                        
                        scenes_text = "\n".join(transcript_lines)
                        
                        custom_style = options.get('custom_prompt', '').strip() if options else ""
                        custom_instruction = f"\n5. CUSTOM STYLE INSTRUCTION: {custom_style}\n" if custom_style else ""
                        
                        prompt = (f"You are a professional movie translator and adapter. "
                                  f"Below is a transcript with exactly {len(batch_segments)} lines of dialogue. "
                                  f"Your job is to REWRITE and ADAPT each line into {lang_label}. "
                                  f"CRITICAL RULES:\n"
                                  f"1. SUMMARIZE TO THE USEFUL CORE: Shorten the dialogue to its most useful and essential meaning so it is quick to speak, but KEEP IT AS A CONVERSATION (do not use 3rd person storytelling).\n"
                                  f"2. You MUST output EXACTLY {len(batch_segments)} lines. Do not skip or combine any lines. Every single line must have a corresponding shortened translation.\n"
                                  f"3. Translate naturally so it sounds like a professional movie dubbing script.\n"
                                  f"4. Write ONLY in {lang_label}. Do NOT output any English.{custom_instruction}\n\n"
                                  f"Output a JSON array of exactly {len(batch_segments)} strings.\n\n"
                                  f"Transcript:\n{scenes_text}")
                        
                        generation_config = genai.types.GenerateContentConfig(response_mime_type="application/json")
                        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt, config=generation_config)
                        
                        try:
                            recap_sentences = json.loads(response.text.strip())
                        except json.JSONDecodeError:
                            # If JSON is truncated, try to manually extract strings
                            import re
                            matches = re.findall(r'"([^"]*)"', response.text.strip())
                            recap_sentences = matches if matches else []
                            
                        if not isinstance(recap_sentences, list):
                            recap_sentences = [recap_sentences]
                            
                        for item in recap_sentences:
                            if isinstance(item, str):
                                clean_sentences.append(item)
                            elif isinstance(item, dict) and 'text' in item:
                                clean_sentences.append(str(item['text']))
                            
                    recap_sentences = clean_sentences if clean_sentences else [str(s) for s in recap_sentences]
                    
                    print(f"[Recap] AI returned {len(recap_sentences)} sentences for {len(translated_segments)} original lines")
                    
                    # Map the storytelling text directly back onto the PERFECT original timestamps
                    for idx, seg in enumerate(translated_segments):
                        if idx < len(recap_sentences):
                            seg['text'] = recap_sentences[idx].strip()
                        
                except Exception as e:
                    print(f"Recap generation failed: {e}")
                    raise Exception(f"Recap generation failed: {str(e)}")
            
        else:
            # ── LOCAL WHISPER PIPELINE ──
            # ── 1. Transcribe using Whisper (or skip if subs found) ───────────────────────
            whisper_task = 'translate' if target_lang == 'English' else 'transcribe'
            
            if original_subs_found:
                pass
            elif 'Premium API' in transcriber_engine:
                if not transcriber_key:
                    raise Exception(f"Please provide an API Key for {transcriber_engine}.")
                
                upd(job_id, stage='transcribe', progress=20,
                    message=f'☁️ Transcribing audio with {transcriber_engine}…')
                
                import requests
                headers = {'Authorization': f'Bearer {transcriber_key}'}
                url = 'https://api.groq.com/openai/v1/audio/transcriptions' if 'Groq' in transcriber_engine else 'https://api.openai.com/v1/audio/transcriptions'
                model_name = 'whisper-large-v3' if 'Groq' in transcriber_engine else 'whisper-1'
                
                with open(audio_path, 'rb') as f:
                    files = {'file': (os.path.basename(audio_path), f, 'audio/wav')}
                    data = {'model': model_name, 'response_format': 'verbose_json'}
                    if 'OpenAI' in transcriber_engine:
                        data['timestamp_granularities[]'] = 'segment'
                    res = requests.post(url, headers=headers, files=files, data=data, timeout=60)
                
                if res.status_code != 200:
                    raise Exception(f"{transcriber_engine} API Error: {res.text}")
                    
                segments = res.json().get('segments', [])
            else:
                speed_str = options.get('speed', 'Whisper Medium (Great, Mod)')
                if "Large" in speed_str:
                    model_size = "large"
                elif "Medium" in speed_str:
                    model_size = "medium"
                else:
                    model_size = "base"
                
                upd(job_id, stage='transcribe', progress=20,
                    message=f'📦 Downloading/Loading Whisper {model_size.upper()} AI... (This takes a few minutes)')
                
                debug_log(f"Loading whisper model: {model_size}")
                import whisper
                model = whisper.load_model(model_size)
                
                debug_log(f"Running whisper transcribe with task: {whisper_task}")
                
                upd(job_id, stage='transcribe', progress=25,
                    message=f'📝 Transcribing audio with {model_size.upper()} AI... (This is very slow on CPU)')
                
                result = model.transcribe(
                    audio_path, 
                    fp16=False, 
                    task=whisper_task,
                    condition_on_previous_text=False
                )
                segments = result.get('segments', [])
            
            if not segments:
                raise Exception("No speech found in the video.")
            debug_log(f"Whisper returned {len(segments)} segments")



            # ── 4. Translate to Target Language ───────────────────
            upd(job_id, stage='translate', progress=48,
                message=f'🌏 Translating segments to {lang_label}…')

            video_file = None

            if translator_engine == 'Gemini AI (Premium API)' and translator_key:
                upd(job_id, message='☁️ Uploading video to Gemini for visual context…')
                from google import genai
                client = genai.Client(api_key=translator_key)
                try:
                    video_file = client.files.upload(file=video_path)
                    upd(job_id, message='⏳ Waiting for Gemini to process video…')
                    while video_file.state.name == 'PROCESSING':
                        check_cancel()
                        time.sleep(2)
                        video_file = client.files.get(name=video_file.name)
                except Exception as e:
                    print(f"[Gemini Video Upload Error] {e}")
                    video_file = None

            translated_segments = []
            
            # Step 1: Base 1:1 Translation
            if target_lang == 'English' and whisper_task == 'translate':
                for seg in segments:
                    translated_segments.append({
                        'start': seg['start'],
                        'end':   seg['end'],
                        'text':  seg['text'].strip()
                    })
            else:
                texts_to_translate = [seg['text'].strip() for seg in segments]
                translated_texts = []
                chunk_size = 50
                
                for i in range(0, len(texts_to_translate), chunk_size):
                    chunk = texts_to_translate[i:i + chunk_size]
                    upd(job_id, message=f'🌏 Translating batch {i//chunk_size + 1}…')
                    
                    if translator_engine == 'Gemini AI (Premium API)' and translator_key:
                        translated_texts.extend(gemini_translate_batch(chunk, human_dest=lang_label, dest_code=dest_code, api_key=translator_key, video_file=video_file))
                    elif translator_engine == 'DeepSeek (Premium API)' and translator_key:
                        translated_texts.extend(deepseek_translate_batch(chunk, human_dest=lang_label, dest_code=dest_code, api_key=translator_key))
                    else:
                        translated_texts.extend(robust_translate_batch(chunk, dest=dest_code))
                    
                    if target_lang != 'English':
                        time.sleep(0.5)
                
                for seg, t_text in zip(segments, translated_texts):
                    translated_segments.append({
                        'start': seg['start'],
                        'end':   seg['end'],
                        'text':  t_text or seg['text'].strip(),
                    })
                
                        
            # Step 2: Dual Dubbing (Recap Injection)
            if options.get('story_mode'):
                check_cancel()
                upd(job_id, stage='translate', progress=48, message=f'📋 Writing AI Recap in {lang_label}…')
                
                if not translator_key:
                    raise Exception("Recap Mode requires a Gemini API Key. Please enter your Gemini API key in the Translator Key box.")
                    
                try:
                    from google import genai
                    import json, math
                    client = genai.Client(api_key=translator_key)
                    
                    # Group original translated segments into scene chunks
                    scene_chunks = []
                    current_chunk = []
                    GAP_THRESHOLD = 2.0  # seconds of silence = new scene
                    
                    for i, seg in enumerate(translated_segments):
                        if not current_chunk:
                            current_chunk.append(seg)
                        else:
                            gap = seg['start'] - current_chunk[-1]['end']
                            if gap > GAP_THRESHOLD:
                                scene_chunks.append(current_chunk)
                                current_chunk = [seg]
                            else:
                                current_chunk.append(seg)
                    if current_chunk:
                        scene_chunks.append(current_chunk)
                    
                    # If too few chunks, split into roughly equal groups
                    if len(scene_chunks) < 3 and len(translated_segments) > 3:
                        scene_chunks = []
                        chunk_size = max(2, len(translated_segments) // 4)
                        for i in range(0, len(translated_segments), chunk_size):
                            scene_chunks.append(translated_segments[i:i+chunk_size])
                    
                    print(f"[Recap] Split {len(translated_segments)} segments into {len(scene_chunks)} scene chunks")
                    
                    # Build prompt with numbered scenes
                    scene_descriptions = []
                    for i, chunk in enumerate(scene_chunks):
                        chunk_text = " ".join([s['text'] for s in chunk])
                        t_start = chunk[0]['start']
                        t_end = chunk[-1]['end']
                        scene_descriptions.append(f"Scene {i+1} [{t_start:.1f}s-{t_end:.1f}s]: {chunk_text}")
                    
                    scenes_text = "\n".join(scene_descriptions)
                    
                    prompt = (f"You are a professional movie recap narrator (like YouTube movie recaps). "
                              f"Below are {len(scene_chunks)} scenes from a movie. "
                              f"Write EXACTLY {len(scene_chunks)} recap sentences — one per scene, in order. "
                              f"Each sentence should describe what happens in that scene. Keep each sentence SHORT (under 25 words). "
                              f"CRITICAL: Write ONLY in {lang_label}. No English.\n\n"
                              f"Output a JSON array of exactly {len(scene_chunks)} strings.\n\n"
                              f"Scenes:\n{scenes_text}")
                    
                    generation_config = genai.types.GenerateContentConfig(response_mime_type="application/json")
                    try:
                        if video_file:
                            response = client.models.generate_content(model='gemini-2.5-flash', contents=[video_file, prompt], config=generation_config)
                        else:
                            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt, config=generation_config)
                    except Exception as e:
                        raise e
                    
                    recap_sentences = json.loads(response.text.strip())
                    if not isinstance(recap_sentences, list):
                        recap_sentences = [recap_sentences]
                    # Clean up
                    clean_sentences = []
                    for item in recap_sentences:
                        if isinstance(item, str):
                            clean_sentences.append(item)
                        elif isinstance(item, dict) and 'text' in item:
                            clean_sentences.append(str(item['text']))
                    recap_sentences = clean_sentences if clean_sentences else [str(s) for s in recap_sentences]
                    
                    print(f"[Recap] AI returned {len(recap_sentences)} sentences for {len(scene_chunks)} scenes")
                    
                    # Inject recap sentences before each scene
                    final_segments = []
                    for idx, scene in enumerate(scene_chunks):
                        if idx < len(recap_sentences):
                            recap_text = recap_sentences[idx]
                            scene_start = scene[0]['start']
                            
                            if idx == 0:
                                recap_start = max(0.0, scene_start - 3.5)
                            else:
                                prev_scene_end = scene_chunks[idx-1][-1]['end']
                                recap_start = max(prev_scene_end + 0.1, scene_start - 3.5)
                                
                            recap_end = scene_start
                            
                            final_segments.append({
                                'start': round(recap_start, 2),
                                'end': round(recap_end, 2),
                                'text': recap_text.strip(),
                                'gender': 'narrator'
                            })
                            
                        # Add the 1:1 translated dialogue segments
                        for seg in scene:
                            final_segments.append(seg)
                            
                    translated_segments = final_segments
                    print(f"[Recap] Final Dual Dubbing segments: {[(s['start'], s.get('gender', 'unknown'), s['text'][:10]) for s in translated_segments]}")
                        
                except Exception as e:
                    print(f"Recap generation failed: {e}")
                    raise Exception(f"Recap generation failed: {str(e)}")
                
                if video_file:
                    try:
                        from google import genai
                        client.files.delete(name=video_file.name)
                    except:
                        pass

            translated_text = " ".join([s['text'] for s in translated_segments])
            
            # Clean up the uploaded video file
            if video_file:
                try:
                    from google import genai
                    client.files.delete(name=video_file.name)
                except:
                    pass

        # ── 5. Generate Text-to-Speech (TTS) ──────────────────
        upd(job_id,
            translated_text=translated_text,
            segments=translated_segments,
            message=f'✅ {len(translated_segments)} segments translated to {lang_label}')

        upd(job_id, stage='tts', progress=60,
            message=f'🎙️ Generating {lang_label} voiceovers…')
            
        def tts_progress(pct, done, total, msg):
            upd(job_id, progress=60 + int((pct/100)*23), message=f'🔊 {msg} — line {done}/{total}…')

        video_duration = AudioSegment.from_file(original_audio_path).duration_seconds
        
        is_story = options.get('story_mode', False)
        # Resolve voice: 'Auto Detect' means use the language default, not literally 'Auto Detect'
        opt_voice_male = options.get('voice_male', '')
        opt_voice_female = options.get('voice_female', '')
        resolved_male = opt_voice_male if (opt_voice_male and opt_voice_male != 'Auto Detect') else voice_male
        resolved_female = opt_voice_female if (opt_voice_female and opt_voice_female != 'Auto Detect') else voice_female
        timed_dub = build_timed_audio(
            translated_segments, bg_audio_path, video_duration, job_id, tts_progress,
            voice_male=resolved_male,
            voice_female=resolved_female,
            options=options, 
            original_audio_path=original_audio_path, 
            video_path=video_path,
            genders=[s.get('gender') for s in translated_segments] if any(s.get('gender') for s in translated_segments) else None,
            sequential_mode=False,
            is_vocals_removed=bool(options.get('remove_vocals'))
        )
        
        upd(job_id, segments=translated_segments, progress=83,
            message=f'✅ Timed {lang_label} audio track ready')

        # ── 5.5 Write SRT ─────────────────
        srt_file = os.path.join(OUTPUT_DIR, f'{job_id}_khmer.srt')
        write_srt(translated_segments, srt_file)

        # ── 6. Merge timed audio ─
        upd(job_id, stage='merge', progress=87,
            message='🎬 Merging timed dubbed audio with video and burning subtitles…')
        out_video = os.path.join(OUTPUT_DIR, f'{job_id}_khmer_dubbed.mp4')
        
        vf_filters = []
        if options.get('mirror'):
            vf_filters.append('hflip')
            
        if options.get('blur_watermark'):
            vf_filters.append('drawbox=x=0:y=0:w=iw:h=ih/9:color=black@0.9:t=fill')
            vf_filters.append('drawbox=x=0:y=ih-ih/9:w=iw:h=ih/9:color=black@0.9:t=fill')

        srt_path_ffmpeg = srt_file.replace('\\', '/').replace(':', '\\:')
        fonts_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        fonts_dir_ffmpeg = fonts_dir.replace('\\', '/').replace(':', '\\:')
        # WeTV-style subtitles: white text, semi-transparent dark box, clean and readable
        style = (f"Fontname={font_name},FontSize=11,PrimaryColour=&H00FFFFFF,"
                 f"OutlineColour=&H80000000,BackColour=&H80000000,"
                 f"Outline=2,Shadow=0,BorderStyle=4,MarginV=25,Alignment=2")
        style_escaped = style.replace(",", "\\,")
        vf_filters.append(f"subtitles='{srt_path_ffmpeg}':fontsdir='{fonts_dir_ffmpeg}':force_style='{style_escaped}'")
        
        ffmpeg_cmd = [
            FFMPEG_CMD, '-i', video_path, '-i', timed_dub
        ]
        ffmpeg_cmd.extend(['-vf', ','.join(vf_filters)])
        ffmpeg_cmd.extend(['-c:v', 'libx264', '-preset', 'fast', '-crf', '24'])
        ffmpeg_cmd.extend(['-map', '0:v:0', '-map', '1:a:0', '-y', out_video])
        
        subprocess.run(ffmpeg_cmd, check=True)

        upd(job_id,
            stage='complete', progress=100, status='complete',
            message='🎉 Done! Khmer dubbed video is ready (lip-sync timed).',
            output_video=f'{job_id}_khmer_dubbed.mp4',
            output_srt=f'{job_id}_khmer.srt')

    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b'').decode(errors='replace')
        err_short = err[-400:].strip()
        debug_log(f"Exception CalledProcessError: {err_short}")
        upd(job_id, stage='error', status='error',
            message=f'❌ FFmpeg error: {err_short}')
    except Exception as exc:
        import traceback
        err_trace = traceback.format_exc()
        debug_log(f"Exception in process_video: {exc}\n{err_trace}")
        upd(job_id, stage='error', status='error',
            message=f'❌ Error: {exc}')
    finally:
        for p in [audio_path, timed_dub]:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass


# ── UI TEXT DICTIONARY ──────────────────────────
UI_TEXT = {
    "en": {
        "title": "BongbeeAI dub",
        "select_local": "Select Local Video",
        "no_video": "No video selected",
        "url_placeholder": "Or paste video URL here (WeTV, YouTube, etc.)",
        "download": "Download Video",
        "lbl_lang": "1. Dub Into:",
        "sel_lang": "Select Language...",
        "lbl_transcriber": "2. Transcriber:",
        "sel_transcriber": "Select Transcriber...",
        "lbl_speed": "Speed:",
        "lbl_transcriber_key": "API Key:",
        "ent_transcriber_key": "API Key...",
        "get_groq_key": "Get Free Groq Key",
        "lbl_trans": "3. Translator:",
        "sel_translator": "Select Translator...",
        "lbl_translator_key": "API Key:",
        "ent_translator_key": "API Key...",
        "lbl_engine": "4. Voice Engine:",
        "sel_engine": "Select Engine...",
        "lbl_voice_speed": "Speed:",
        "lbl_key": "KiriTTS Key:",
        "ent_key": "sk-...",
        "lbl_profile": "Profile:",
        "ent_profile": "Profile name",
        "save_keys": "💾 Save Keys",
        "lbl_male": "Male Voice:",
        "auto_detect": "Auto Detect",
        "lbl_female": "Female Voice:",
        "lbl_dub_gender": "Dub As:",
        "male_only": "Male Only",
        "female_only": "Female Only",
        "chk_story": "Dual Dubbing (Recap+Voice)",
        "chk_vocals": "Remove Vocals",
        "chk_mirror": "Mirror Video",
        "chk_blur": "Hide Watermarks",
        "start_dubbing": "Start Dubbing",
        "ready": "Ready",
        "ui_lang": "UI Lang:"
    },
    "km": {
        "title": "BongbeeAI dub",
        "select_local": "ជ្រើសរើសវីដេអូ",
        "no_video": "មិនមានវីដេអូទេ",
        "url_placeholder": "ឬដាក់តំណរវីដេអូ (WeTV, YouTube...)",
        "download": "ទាញយកវីដេអូ",
        "lbl_lang": "១. បញ្ចូលសំឡេងជា:",
        "sel_lang": "ជ្រើសរើសភាសា...",
        "lbl_transcriber": "២. បំប្លែងសំឡេង:",
        "sel_transcriber": "ជ្រើសរើសកម្មវិធី...",
        "lbl_speed": "ល្បឿន:",
        "lbl_transcriber_key": "សោ API:",
        "ent_transcriber_key": "សោ API...",
        "get_groq_key": "យកសោ Groq ឥតគិតថ្លៃ",
        "lbl_trans": "៣. អ្នកបកប្រែ:",
        "sel_translator": "ជ្រើសរើសអ្នកបកប្រែ...",
        "lbl_translator_key": "សោ API:",
        "ent_translator_key": "សោ API...",
        "lbl_engine": "៤. កម្មវិធីសំឡេង:",
        "sel_engine": "ជ្រើសរើសកម្មវិធី...",
        "lbl_voice_speed": "ល្បឿន:",
        "lbl_key": "សោ KiriTTS:",
        "ent_key": "sk-...",
        "lbl_profile": "ទម្រង់:",
        "ent_profile": "ឈ្មោះទម្រង់",
        "save_keys": "💾 រក្សាទុក",
        "lbl_male": "សំឡេងប្រុស:",
        "auto_detect": "ស្វែងរកស្វ័យប្រវត្តិ",
        "lbl_female": "សំឡេងស្រី:",
        "lbl_dub_gender": "បញ្ចូលជា:",
        "male_only": "ប្រុសប៉ុណ្ណោះ",
        "female_only": "ស្រីប៉ុណ្ណោះ",
        "chk_story": "បញ្ចូលសំឡេងពីរ (សង្ខេប+សន្ទនា)",
        "chk_vocals": "ដកសំឡេងច្រៀងចេញ",
        "chk_mirror": "ត្រឡប់វីដេអូ (Mirror)",
        "chk_blur": "លុបឡូហ្គោ",
        "start_dubbing": "ចាប់ផ្តើមបញ្ចូលសំឡេង",
        "ready": "រួចរាល់",
        "ui_lang": "ភាសា UI:"
    }
}

class KhmerDubApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        global app_gui
        app_gui = self  
        self.title("BongbeeAI dub")
        import threading
        self.cancel_event = threading.Event()
        self.geometry("700x650")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.video_path = None
        
        try:
            self.iconbitmap(get_bin_path('icon.ico'))
        except: pass
        
        try:
            ctk.FontManager.load_font(get_bin_path('Battambang-Regular.ttf'))
        except Exception as e:
            debug_log(f"Failed to load Battambang font: {e}")
            
        # UI Layout
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(pady=20, padx=20, fill="both", expand=True)
        
        self.ui_lang_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.ui_lang_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.lbl_ui_lang = ctk.CTkLabel(self.ui_lang_frame, text="UI Lang:", font=("Segoe UI", 12))
        self.lbl_ui_lang.pack(side="right", padx=5)
        self.ui_lang_var = ctk.StringVar(value="en")
        self.opt_ui_lang = ctk.CTkOptionMenu(self.ui_lang_frame, variable=self.ui_lang_var, values=["en", "km"], font=("Segoe UI", 12), width=60, command=self.update_ui_language)
        self.opt_ui_lang.pack(side="right")
        
        self.lbl_title = ctk.CTkLabel(self.main_frame, text="BongbeeAI dub", font=("Segoe UI", 24, "bold"))
        self.lbl_title.pack(pady=15)
        
        self.file_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.file_frame.pack(pady=10, fill="x")
        
        self.btn_select = ctk.CTkButton(self.file_frame, text="Select Local Video", command=self.select_video, font=("Segoe UI", 14), width=180)
        self.btn_select.pack(side="left", padx=10)
        
        self.lbl_file = ctk.CTkLabel(self.file_frame, text="No video selected", font=("Segoe UI", 12), text_color="gray")
        self.lbl_file.pack(side="left", padx=10)
        
        self.url_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.url_frame.pack(pady=5, fill="x")
        
        self.url_entry = ctk.CTkEntry(self.url_frame, placeholder_text="Or paste video URL here (WeTV, YouTube, etc.)", font=("Segoe UI", 12), width=350)
        self.url_entry.pack(side="left", padx=10)
        
        self.btn_download = ctk.CTkButton(self.url_frame, text="Download Video", command=self.start_download, font=("Segoe UI", 14), width=140, fg_color="#17a2b8", hover_color="#138496")
        self.btn_download.pack(side="left", padx=5)
        
        self.lang_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lang_frame.pack(pady=5, fill="x")
        self.lbl_lang = ctk.CTkLabel(self.lang_frame, text="1. Dub Into:", font=("Segoe UI", 14, "bold"))
        self.lbl_lang.pack(side="left", padx=10)
        self.lang_var = ctk.StringVar(value="Select Language...")
        self.opt_lang = ctk.CTkOptionMenu(self.lang_frame, variable=self.lang_var, values=["Select Language...", "Khmer", "English", "Chinese"], font=("Segoe UI", 14), command=self.update_ui_visibility)
        self.opt_lang.pack(side="left", padx=5)
        
        self.transcriber_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_transcriber = ctk.CTkLabel(self.transcriber_frame, text="2. Transcriber:", font=("Segoe UI", 14, "bold"))
        self.lbl_transcriber.pack(side="left", padx=10)
        self.transcriber_var = ctk.StringVar(value="Select Transcriber...")
        self.opt_transcriber = ctk.CTkOptionMenu(self.transcriber_frame, variable=self.transcriber_var, values=["Select Transcriber...", "Whisper Local (Free)", "Gemini AI (Premium API)", "Groq Whisper (Premium API)", "OpenAI Whisper (Premium API)"], font=("Segoe UI", 14), width=250, command=self.update_ui_visibility)
        self.opt_transcriber.pack(side="left", padx=5)
        
        self.lbl_speed = ctk.CTkLabel(self.transcriber_frame, text="Speed:", font=("Segoe UI", 14, "bold"))
        self.speed_var = ctk.StringVar(value="Whisper Medium (Great, Mod)")
        self.opt_speed = ctk.CTkOptionMenu(self.transcriber_frame, variable=self.speed_var, values=["Whisper Medium (Great, Mod)", "Whisper Base (Okay, Fast)"], font=("Segoe UI", 14), width=180)
        
        self.transcriber_api_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_transcriber_key = ctk.CTkLabel(self.transcriber_api_frame, text="API Key:", font=("Segoe UI", 14, "bold"))
        self.lbl_transcriber_key.pack(side="left", padx=10)
        self.transcriber_key_var = ctk.StringVar(value="")
        self.ent_transcriber_key = ctk.CTkEntry(self.transcriber_api_frame, textvariable=self.transcriber_key_var, placeholder_text="API Key...", show="*", width=250, font=("Segoe UI", 14))
        self.ent_transcriber_key.pack(side="left", padx=5)
        self.btn_get_groq_key = ctk.CTkButton(self.transcriber_api_frame, text="Get Free Groq Key", font=("Segoe UI", 12), width=120, fg_color="#f39c12", hover_color="#d68910", command=lambda: __import__('webbrowser').open('https://console.groq.com/keys'))
        
        self.trans_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_trans = ctk.CTkLabel(self.trans_frame, text="3. Translator:", font=("Segoe UI", 14, "bold"))
        self.lbl_trans.pack(side="left", padx=10)
        self.trans_var = ctk.StringVar(value="Select Translator...")
        self.opt_trans = ctk.CTkOptionMenu(self.trans_frame, variable=self.trans_var, values=["Select Translator...", "Google Translate (Free)", "Gemini AI (Premium API)", "DeepSeek (Premium API)"], font=("Segoe UI", 14), width=230, command=self.update_ui_visibility)
        self.opt_trans.pack(side="left", padx=5)
        
        self.translator_api_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_translator_key = ctk.CTkLabel(self.translator_api_frame, text="API Key:", font=("Segoe UI", 14, "bold"))
        self.lbl_translator_key.pack(side="left", padx=10)
        self.translator_key_var = ctk.StringVar(value="")
        self.ent_translator_key = ctk.CTkEntry(self.translator_api_frame, textvariable=self.translator_key_var, placeholder_text="API Key...", show="*", width=250, font=("Segoe UI", 14))
        self.ent_translator_key.pack(side="left", padx=5)
        
        self.engine_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_engine = ctk.CTkLabel(self.engine_frame, text="4. Voice Engine:", font=("Segoe UI", 14, "bold"))
        self.lbl_engine.pack(side="left", padx=10)
        self.engine_var = ctk.StringVar(value="Select Engine...")
        self.opt_engine = ctk.CTkOptionMenu(self.engine_frame, variable=self.engine_var, values=["Select Engine...", "Edge-TTS", "KiriTTS"], font=("Segoe UI", 14), command=self.update_ui_visibility)
        self.opt_engine.pack(side="left", padx=5)
        
        self.lbl_voice_speed = ctk.CTkLabel(self.engine_frame, text="Speed:", font=("Segoe UI", 14, "bold"))
        self.voice_speed_var = ctk.StringVar(value="1.0x")
        self.opt_voice_speed = ctk.CTkOptionMenu(self.engine_frame, variable=self.voice_speed_var, values=["1.0x", "1.25x", "1.5x", "1.75x", "2.0x"], font=("Segoe UI", 14), width=80)
        
        self.lbl_key = ctk.CTkLabel(self.engine_frame, text="KiriTTS Key:", font=("Segoe UI", 14, "bold"))
        self.api_key_var = ctk.StringVar(value="")
        self.ent_key = ctk.CTkEntry(self.engine_frame, textvariable=self.api_key_var, placeholder_text="sk-...", show="*", width=150, font=("Segoe UI", 14))
        self.api_key_var.trace_add("write", lambda *args: self.fetch_kiritts_voices() if self.engine_var.get() == "KiriTTS" else None)

        # Profile name for saving / loading API keys
        self.profile_name_var = ctk.StringVar(value="default")
        self.lbl_profile = ctk.CTkLabel(self.engine_frame, text="Profile:", font=("Segoe UI", 14, "bold"))
        self.ent_profile = ctk.CTkEntry(self.engine_frame, textvariable=self.profile_name_var, placeholder_text="Profile name", width=120, font=("Segoe UI", 14))
        self.btn_save_keys = ctk.CTkButton(self.engine_frame, text="💾 Save Keys", command=self.save_api_keys, font=("Segoe UI", 12), width=110, fg_color="#28a745", hover_color="#218838")

        self.voice_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_male = ctk.CTkLabel(self.voice_frame, text="Male Voice:", font=("Segoe UI", 14, "bold"))
        self.lbl_male.pack(side="left", padx=10)
        self.voice_male_var = ctk.StringVar(value="Auto Detect")
        self.ent_male = ctk.CTkComboBox(self.voice_frame, variable=self.voice_male_var, width=160, font=("Segoe UI", 14), values=["Auto Detect"])
        self.ent_male.pack(side="left", padx=5)
        self.lbl_female = ctk.CTkLabel(self.voice_frame, text="Female Voice:", font=("Segoe UI", 14, "bold"))
        self.lbl_female.pack(side="left", padx=(15, 5))
        self.voice_female_var = ctk.StringVar(value="Auto Detect")
        self.ent_female = ctk.CTkComboBox(self.voice_frame, variable=self.voice_female_var, width=160, font=("Segoe UI", 14), values=["Auto Detect"])
        self.ent_female.pack(side="left", padx=5)
        # Dub gender override
        self.lbl_dub_gender = ctk.CTkLabel(self.voice_frame, text="Dub As:", font=("Segoe UI", 14, "bold"))
        self.lbl_dub_gender.pack(side="left", padx=(15, 5))
        self.dub_gender_var = ctk.StringVar(value="Auto Detect")
        self.opt_dub_gender = ctk.CTkOptionMenu(self.voice_frame, variable=self.dub_gender_var,
                                                values=["Auto Detect", "Male Only", "Female Only"],
                                                font=("Segoe UI", 14), width=130)
        self.opt_dub_gender.pack(side="left", padx=5)
        
        self.chk_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.chk_mirror_var = ctk.StringVar(value="off")
        self.chk_blur_var = ctk.StringVar(value="off")
        self.chk_emotion_var = ctk.StringVar(value="on")
        self.chk_vocals_var = ctk.StringVar(value="off")
        self.chk_story_var = ctk.StringVar(value="off")
        self.chk_sync_var = ctk.StringVar(value="on")
        
        self.chk_story = ctk.CTkCheckBox(self.chk_frame, text="Generate Recap", variable=self.chk_story_var, onvalue="on", offvalue="off", fg_color="#ffc107", hover_color="#e0a800")
        self.chk_story.pack(side="left", padx=10)
        self.chk_vocals = ctk.CTkCheckBox(self.chk_frame, text="Remove Vocals", variable=self.chk_vocals_var, onvalue="on", offvalue="off")
        self.chk_vocals.pack(side="left", padx=10)
        self.chk_sync = ctk.CTkCheckBox(self.chk_frame, text="Exact Lip Sync", variable=self.chk_sync_var, onvalue="on", offvalue="off")
        self.chk_sync.pack(side="left", padx=10)
        self.chk_mirror = ctk.CTkCheckBox(self.chk_frame, text="Mirror Video", variable=self.chk_mirror_var, onvalue="on", offvalue="off")
        self.chk_mirror.pack(side="left", padx=10)
        self.chk_blur = ctk.CTkCheckBox(self.chk_frame, text="Hide Watermarks", variable=self.chk_blur_var, onvalue="on", offvalue="off")
        self.chk_blur.pack(side="left", padx=10)

        self.custom_prompt_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lbl_custom_prompt = ctk.CTkLabel(self.custom_prompt_frame, text="Custom AI Prompt:", font=("Segoe UI", 14, "bold"))
        self.lbl_custom_prompt.pack(side="left", padx=10)
        self.custom_prompt_var = ctk.StringVar(value="")
        self.ent_custom_prompt = ctk.CTkEntry(self.custom_prompt_frame, textvariable=self.custom_prompt_var, width=500, placeholder_text="Optional: Paste a custom style (e.g. 'Make it an interactive medical game...')", font=("Segoe UI", 12))
        self.ent_custom_prompt.pack(side="left", padx=10)

        
        self.btn_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.btn_frame.pack(pady=25)
        self.btn_start = ctk.CTkButton(self.btn_frame, text="Start Dubbing", command=self.start_dubbing, font=("Segoe UI", 18, "bold"), fg_color="#28a745", hover_color="#218838", height=45)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = ctk.CTkButton(self.btn_frame, text="Stop Process", command=self.stop_dubbing, font=("Segoe UI", 18, "bold"), fg_color="#dc3545", hover_color="#c82333", height=45, state="disabled")
        self.btn_stop.pack(side="left", padx=10)
        
        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=500, height=20)
        self.progress_bar.pack(pady=10)
        self.progress_bar.set(0)
        
        self.lbl_status = ctk.CTkLabel(self.main_frame, text="Ready", font=("Segoe UI", 14), text_color="#17a2b8")
        self.lbl_status.pack(pady=10)
        
        # Initialize dynamic visibility
        self._load_api_keys()
        self.update_ui_visibility()
        self.update_ui_language()
        
    def select_video(self):
        path = filedialog.askopenfilename(filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv")])
        if path:
            self.video_path = path
            self.lbl_file.configure(text=os.path.basename(path))
            
    def start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Error", "Please paste a URL first!")
            return
            
        self.btn_download.configure(state="disabled", text="Downloading...")
        self.lbl_status.configure(text="Downloading video... please wait.")
        self.progress_bar.set(0)
        
        def download_thread():
            try:
                def progress_hook(d):
                    if d['status'] == 'downloading':
                        try:
                            p = d.get('_percent_str', '0%').replace('%', '').strip()
                            import re
                            p = re.sub(r'\x1b\[[0-9;]*m', '', p)
                            pct = float(p)
                            self.after(0, lambda: self.progress_bar.set(pct / 100.0))
                            
                            speed = d.get('_speed_str', 'N/A')
                            speed = re.sub(r'\x1b\[[0-9;]*m', '', speed).strip()
                            self.after(0, lambda: self.lbl_status.configure(text=f"Downloading... {pct}% ({speed})"))
                        except Exception:
                            pass
                    elif d['status'] == 'finished':
                        self.after(0, lambda: self.lbl_status.configure(text="Download finished. Processing..."))

                ydl_opts = {
                    'format': 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': os.path.join(TEMP_DIR, 'downloaded_%(id)s.%(ext)s'),
                    'ffmpeg_location': FFMPEG_CMD,
                    'progress_hooks': [progress_hook],
                    'quiet': True,
                    'no_warnings': True,
                    'source_address': '0.0.0.0', 
                    'socket_timeout': 30,
                    'retries': 10,
                    'fragment_retries': 10,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['en', 'all'],
                    'subtitleslangs': ['en', 'en-US', 'en-GB', 'zh-cn', 'th', 'id', 'vi', 'ko', 'ja', 'all'],
                    'subtitlesformat': 'vtt/srt/best'
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if 'requested_downloads' in info and len(info['requested_downloads']) > 0:
                        filepath = info['requested_downloads'][0].get('filepath') or ydl.prepare_filename(info)
                    else:
                        filepath = ydl.prepare_filename(info)
                    if not os.path.exists(filepath):
                        base = os.path.splitext(filepath)[0]
                        import glob
                        matches = glob.glob(base + ".*")
                        if matches:
                            filepath = matches[0]
                    
                def on_success():
                    self.video_path = filepath
                    self.lbl_file.configure(text=f"Downloaded: {os.path.basename(filepath)}")
                    self.lbl_status.configure(text="Video ready! Starting dubbing automatically...")
                    self.btn_download.configure(state="normal", text="Download Video")
                    self.progress_bar.set(0)
                    self.start_dubbing()
                    
                self.after(0, on_success)
            except Exception as e:
                import traceback
                print(traceback.format_exc())
                err_msg = getattr(e, 'msg', str(e))
                if not err_msg or err_msg == "None":
                    err_msg = repr(e)
                def on_error():
                    messagebox.showerror("Download Error", f"Failed to download video:\n{err_msg}")
                    self.btn_download.configure(state="normal", text="Download Video")
                    self.lbl_status.configure(text="Ready")
                self.after(0, on_error)
                
        threading.Thread(target=download_thread, daemon=True).start()

    def _load_api_keys(self):
        """Load saved API keys from disk and populate UI fields (uses 'default' profile)."""
        if not os.path.exists(API_KEYS_PATH):
            return
        try:
            with open(API_KEYS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            profile = self.profile_name_var.get().strip() or "default"
            profile_data = data.get(profile, {})
            self.api_key_var.set(profile_data.get("kiritts_key", ""))
            self.transcriber_key_var.set(profile_data.get("transcriber_key", ""))
            self.translator_key_var.set(profile_data.get("translator_key", ""))
        except Exception as e:
            debug_log(f"[Load API Keys] Failed: {e}")

    def update_ui_language(self, choice=None):
        lang = self.ui_lang_var.get()
        t = UI_TEXT.get(lang, UI_TEXT["en"])
        font = "Battambang" if lang == "km" else "Segoe UI"
        
        self.title(t["title"])
        self.lbl_title.configure(text=t["title"], font=(font, 24, "bold"))
        self.btn_select.configure(text=t["select_local"], font=(font, 14))
        self.url_entry.configure(placeholder_text=t["url_placeholder"], font=(font, 12))
        if "Downloading" not in self.btn_download.cget("text"):
            self.btn_download.configure(text=t["download"], font=(font, 14))
            
        self.lbl_lang.configure(text=t["lbl_lang"], font=(font, 14, "bold"))
        self.lbl_transcriber.configure(text=t["lbl_transcriber"], font=(font, 14, "bold"))
        self.lbl_speed.configure(text=t["lbl_speed"], font=(font, 14, "bold"))
        self.lbl_transcriber_key.configure(text=t["lbl_transcriber_key"], font=(font, 14, "bold"))
        self.ent_transcriber_key.configure(placeholder_text=t["ent_transcriber_key"], font=(font, 14))
        self.btn_get_groq_key.configure(text=t["get_groq_key"], font=(font, 12))
        
        self.lbl_trans.configure(text=t["lbl_trans"], font=(font, 14, "bold"))
        self.lbl_translator_key.configure(text=t["lbl_translator_key"], font=(font, 14, "bold"))
        self.ent_translator_key.configure(placeholder_text=t["ent_translator_key"], font=(font, 14))
        
        self.lbl_engine.configure(text=t["lbl_engine"], font=(font, 14, "bold"))
        self.lbl_voice_speed.configure(text=t["lbl_voice_speed"], font=(font, 14, "bold"))
        self.lbl_key.configure(text=t["lbl_key"], font=(font, 14, "bold"))
        self.ent_key.configure(placeholder_text=t["ent_key"], font=(font, 14))
        self.lbl_profile.configure(text=t["lbl_profile"], font=(font, 14, "bold"))
        self.ent_profile.configure(placeholder_text=t["ent_profile"], font=(font, 14))
        self.btn_save_keys.configure(text=t["save_keys"], font=(font, 12))
        
        self.lbl_male.configure(text=t["lbl_male"], font=(font, 14, "bold"))
        self.lbl_female.configure(text=t["lbl_female"], font=(font, 14, "bold"))
        self.lbl_dub_gender.configure(text=t["lbl_dub_gender"], font=(font, 14, "bold"))
        
        self.chk_story.configure(text=t["chk_story"], font=(font, 12))
        self.chk_vocals.configure(text=t["chk_vocals"], font=(font, 12))
        self.chk_mirror.configure(text=t["chk_mirror"], font=(font, 12))
        self.chk_blur.configure(text=t["chk_blur"], font=(font, 12))
        
        if self.btn_start.cget("state") == "normal":
            self.btn_start.configure(text=t["start_dubbing"])
        self.btn_start.configure(font=(font, 18, "bold"))
        
        self.lbl_ui_lang.configure(text=t["ui_lang"], font=(font, 12))
        
        # Make dropdown fonts match language
        self.opt_lang.configure(font=(font, 14))
        self.opt_transcriber.configure(font=(font, 14))
        self.opt_speed.configure(font=(font, 14))
        self.opt_trans.configure(font=(font, 14))
        self.opt_engine.configure(font=(font, 14))
        self.opt_voice_speed.configure(font=(font, 14))
        self.ent_male.configure(font=(font, 14))
        self.ent_female.configure(font=(font, 14))
        self.opt_dub_gender.configure(font=(font, 14))

    def save_api_keys(self):
        """Save current API keys under the given profile name to api_keys.json."""
        profile = self.profile_name_var.get().strip() or "default"
        entry = {
            "kiritts_key": self.api_key_var.get().strip(),
            "transcriber_key": self.transcriber_key_var.get().strip(),
            "translator_key": self.translator_key_var.get().strip(),
        }
        data = {}
        if os.path.exists(API_KEYS_PATH):
            try:
                with open(API_KEYS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[profile] = entry
        try:
            with open(API_KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            messagebox.showinfo("Saved", f"API keys saved under profile '{profile}'.")
        except Exception as e:
            debug_log(f"[Save API Keys] Failed: {e}")
            messagebox.showerror("Error", f"Failed to save API keys:\n{e}")

    def update_status(self, kw):
        if 'progress' in kw:
            self.progress_bar.set(kw['progress'] / 100.0)
        if 'message' in kw:
            self.lbl_status.configure(text=kw['message'])
        if kw.get('status') == 'complete':
            self.btn_start.configure(state="normal", text="Start Dubbing")
            if hasattr(self, 'btn_stop'):
                self.btn_stop.configure(state="disabled", text="Stop Process")
            out = kw.get('output_video')
            messagebox.showinfo("Success", f"Done! Video saved as:\n{out}")
            try:
                subprocess.Popen(f'explorer "{OUTPUT_DIR}"')
            except: pass
        elif kw.get('status') == 'error':
            self.btn_start.configure(state="normal", text="Start Dubbing")
            if hasattr(self, 'btn_stop'):
                self.btn_stop.configure(state="disabled", text="Stop Process")
            messagebox.showerror("Error", kw.get('message'))

    def update_ui_visibility(self, choice=None):
        lang = self.lang_var.get()
        transcriber = self.transcriber_var.get()
        translator = self.trans_var.get()
        engine = self.engine_var.get()

        if "Select" not in lang and choice is not None:
            self.update_voice_defaults()

        # Hide all cascade frames
        self.transcriber_frame.pack_forget()
        self.transcriber_api_frame.pack_forget()
        self.trans_frame.pack_forget()
        self.translator_api_frame.pack_forget()
        self.engine_frame.pack_forget()
        self.voice_frame.pack_forget()
        self.chk_frame.pack_forget()
        self.btn_start.pack_forget()
        
        # Step 1: Language -> Transcriber
        if "Select" not in lang:
            self.transcriber_frame.pack(pady=5, fill="x")
            
            # Step 2: Transcriber -> Transcriber API + Translator
            if "Select" not in transcriber:
                if "Free" in transcriber:
                    self.lbl_speed.pack(side="left", padx=(20, 10))
                    self.opt_speed.pack(side="left", padx=5)
                else:
                    self.lbl_speed.pack_forget()
                    self.opt_speed.pack_forget()
                    self.transcriber_api_frame.pack(pady=5, fill="x")
                    if "Groq" in transcriber:
                        self.lbl_transcriber_key.configure(text="Groq API Key:")
                        self.ent_transcriber_key.configure(placeholder_text="gsk_...")
                        self.btn_get_groq_key.pack(side="left", padx=5)
                    elif "OpenAI" in transcriber:
                        self.lbl_transcriber_key.configure(text="OpenAI API Key:")
                        self.ent_transcriber_key.configure(placeholder_text="sk-proj...")
                        self.btn_get_groq_key.pack_forget()
                    elif "Gemini" in transcriber:
                        self.lbl_transcriber_key.configure(text="Gemini API Key:")
                        self.ent_transcriber_key.configure(placeholder_text="AIzaSy...")
                        self.btn_get_groq_key.pack_forget()
                    
                self.trans_frame.pack(pady=5, fill="x")
                
                # Step 3: Translator -> Translator API + Engine
                if "Select" not in translator:
                    if "Premium" in translator:
                        self.translator_api_frame.pack(pady=5, fill="x")
                        if "DeepSeek" in translator:
                            self.lbl_translator_key.configure(text="DeepSeek API Key:")
                            self.ent_translator_key.configure(placeholder_text="sk-...")
                        elif "Gemini" in translator:
                            self.lbl_translator_key.configure(text="Gemini API Key:")
                            self.ent_translator_key.configure(placeholder_text="AIzaSy...")
                            
                    self.engine_frame.pack(pady=5, fill="x")
                    
                    # Step 4: Engine -> Start
                    if "Select" not in engine:
                        self.lbl_voice_speed.pack(side="left", padx=(15, 5))
                        self.opt_voice_speed.pack(side="left", padx=5)
                        if engine == "KiriTTS":
                            self.opt_voice_speed.configure(state="normal")
                        else:
                            self.opt_voice_speed.configure(state="normal")
                        if engine == "KiriTTS":
                            self.lbl_key.pack(side="left", padx=(20, 10))
                            self.ent_key.pack(side="left", padx=5)
                            self.lbl_profile.pack(side="left", padx=(15, 5))
                            self.ent_profile.pack(side="left", padx=5)
                            self.btn_save_keys.pack(side="left", padx=5)
                            self.voice_frame.pack(pady=5, fill="x")
                        else:
                            self.lbl_key.pack_forget()
                            self.ent_key.pack_forget()
                            self.lbl_profile.pack_forget()
                            self.ent_profile.pack_forget()
                            self.btn_save_keys.pack_forget()
                            self.voice_frame.pack_forget()
                            
                        self.chk_frame.pack(pady=15, fill="x")
                        self.custom_prompt_frame.pack(pady=5, fill="x")
                        self.btn_start.pack(pady=25)
                        
        self.progress_bar.pack_forget()
        self.lbl_status.pack_forget()
        self.progress_bar.pack(pady=10)
        self.lbl_status.pack(pady=10)

    def update_voice_defaults(self, choice=None):
        engine = self.engine_var.get()
        lang = self.lang_var.get()
        if engine == "KiriTTS":
            self.voice_male_var.set("Auto Detect")
            self.voice_female_var.set("Auto Detect")
            self.ent_male.configure(values=["Auto Detect"] + KIRI_MALE_VOICES)
            self.ent_female.configure(values=["Auto Detect"] + KIRI_FEMALE_VOICES)
            self.fetch_kiritts_voices()
        else:
            if lang == "Khmer":
                self.voice_male_var.set("km-KH-PisethNeural")
                self.voice_female_var.set("km-KH-SreymomNeural")
                self.ent_male.configure(values=["km-KH-PisethNeural"])
                self.ent_female.configure(values=["km-KH-SreymomNeural"])
            elif lang == "English":
                self.voice_male_var.set("en-US-GuyNeural")
                self.voice_female_var.set("en-US-AriaNeural")
                self.ent_male.configure(values=["en-US-GuyNeural"])
                self.ent_female.configure(values=["en-US-AriaNeural"])
            elif lang == "Chinese":
                self.voice_male_var.set("zh-CN-YunxiNeural")
                self.voice_female_var.set("zh-CN-XiaoxiaoNeural")
                self.ent_male.configure(values=["zh-CN-YunxiNeural"])
                self.ent_female.configure(values=["zh-CN-XiaoxiaoNeural"])
                
    def fetch_kiritts_voices(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            return
        def fetch_thread():
            import requests
            global KIRI_MALE_VOICES, KIRI_FEMALE_VOICES
            try:
                headers = {'Authorization': f'Bearer {api_key}'}
                res = requests.get('https://api.kiritts.com/v1/voices', headers=headers, timeout=10)
                if res.status_code == 200:
                    data = res.json().get('data', [])
                    debug_log(f"[KiriTTS Voices] Raw API response: {data}")
                    males = [v['name'] for v in data if v.get('gender') == 'male']
                    females = [v['name'] for v in data if v.get('gender') == 'female']
                    # Include cloned/custom voices in the male list
                    # (API may return gender='clone', 'custom', None, or missing for user-created voices)
                    clones = [v['name'] for v in data
                              if v.get('gender') not in ('male', 'female') and v.get('name')]
                    if clones:
                        debug_log(f"[KiriTTS Voices] Found clone/custom voices: {clones}")
                        males = males + [f"{n} (Clone)" if not n.endswith('(Clone)') else n for n in clones]
                    if males:
                        KIRI_MALE_VOICES = males
                    if females:
                        KIRI_FEMALE_VOICES = females

                    self.after(0, lambda: self.ent_male.configure(values=["Auto Detect"] + KIRI_MALE_VOICES))
                    self.after(0, lambda: self.ent_female.configure(values=["Auto Detect"] + KIRI_FEMALE_VOICES))
                else:
                    debug_log(f"[KiriTTS Voices] API error {res.status_code}: {res.text}")
            except Exception as e:
                debug_log(f"[fetch_kiritts_voices] Failed to fetch voices: {e}")
                
        threading.Thread(target=fetch_thread, daemon=True).start()
            
    def stop_dubbing(self):
        self.cancel_event.set()
        self.btn_stop.configure(state="disabled", text="Stopping...")

    def start_dubbing(self):
        if not self.video_path:
            messagebox.showerror("Error", "Please select a video first!")
            return
            
        self.cancel_event.clear()
        self.btn_start.configure(state="disabled", text="Processing...")
        if hasattr(self, 'btn_stop'):
            self.btn_stop.configure(state="normal", text="Stop Process")
        self.progress_bar.set(0)
        self.lbl_status.configure(text="⚙️ Initializing AI components...")
        job_id = str(uuid.uuid4())[:8]
        options = {
            'mirror': self.chk_mirror_var.get() == "on",
            'blur': self.chk_blur_var.get() == "on",
            'smart_emotion': self.chk_emotion_var.get() == "on",
            'remove_vocals': self.chk_vocals_var.get() == "on",
            'custom_prompt': self.custom_prompt_var.get().strip(),
            'target_lang': self.lang_var.get(),
            'transcriber': self.transcriber_var.get(),
            'translator': self.trans_var.get(),
            'speed': self.speed_var.get(),
            'voice_speed': self.voice_speed_var.get(),
            'tts_engine': self.engine_var.get(),
            'kiritts_key': self.api_key_var.get(),
            'transcriber_key': self.transcriber_key_var.get().strip(),
            'translator_key': self.translator_key_var.get().strip(),
            'voice_male': self.voice_male_var.get(),
            'voice_female': self.voice_female_var.get(),
            'story_mode': self.chk_story_var.get() == "on",
            'auto_sync': self.chk_sync_var.get() == "on",
            'dub_gender': self.dub_gender_var.get()  # 'Auto Detect', 'Male Only', or 'Female Only'
        }
        
        def run_with_error_catch():
            try:
                debug_log("Background thread started, calling process_video")
                process_video(job_id, self.video_path, options)
                debug_log("process_video returned")
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                debug_log(f"Exception in run_with_error_catch: {err}")
                print(err)
                self.after(0, lambda: [
                    self.btn_start.configure(state="normal", text="Start Dubbing"),
                    self.lbl_status.configure(text=f"❌ Error: {e}"),
                    messagebox.showerror("Pipeline Error", f"{e}\n\n{err[-500:]}")
                ])
        
        debug_log("Spawning background thread...")
        threading.Thread(target=run_with_error_catch, daemon=True).start()

# ── Standalone run ────────────────────────────────────────────
if __name__ == '__main__':
    app = KhmerDubApp()
    app.mainloop()
