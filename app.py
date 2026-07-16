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
from tkinter import filedialog, messagebox

import yt_dlp
import whisper
from deep_translator import GoogleTranslator
import edge_tts
import librosa
import numpy as np
from pydub import AudioSegment
import cv2
from PIL import Image
import google.generativeai as genai

# ── Runtime directories ───────────
_BASE      = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'KhmerDub_Output')
TEMP_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'KhmerDub', 'temp')
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
_ffmpeg_bin = os.path.join(_BASE, 'ffmpeg_bin')
if os.path.isdir(_ffmpeg_bin):
    os.environ['PATH'] = _ffmpeg_bin + os.pathsep + os.environ.get('PATH', '')

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
                
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-2.5-flash')
                response = model.generate_content([
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
KIRI_MALE_VOICES   = ['Chanda', 'Bora', 'Arun', 'Oudom', 'Rithy', 'Setha']
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
        for cluster, counts in cluster_genders.items():
            majority_gender = 'male' if counts['male'] >= counts['female'] else 'female'
            cluster_voice_map[cluster] = voice_male if majority_gender == 'male' else voice_female

        # Build final voice list aligned to all segments
        voices = []
        for i, seg in enumerate(segments):
            if i in valid_idx:
                pos = valid_idx.index(i)
                fallback_voice = voice_male if (genders[i] or 'male') == 'male' else voice_female
                voices.append(cluster_voice_map.get(labels[pos], fallback_voice))
            else:
                fallback_voice = voice_male if (genders[i] or 'male') == 'male' else voice_female
                voices.append(fallback_voice)

        print(f"[Speaker Cluster] Detected {n_clusters} unique characters → voices: {cluster_voice_map}")
        return voices

    except Exception as e:
        print(f"[Speaker Cluster Error] {e} — using fallback")
        return _fallback_voices(genders, voice_male, voice_female)


def _fallback_voices(genders: list, voice_male: str = 'Rithy', voice_female: str = 'Maly') -> list:
    """Fallback if clustering fails: directly map male/female."""
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
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


# ─────────────────────────────────────────────────────────────
#  AUDIO SPEED ADJUST  (pydub frame-rate trick)
# ─────────────────────────────────────────────────────────────
def speed_change(sound: AudioSegment, speed: float) -> AudioSegment:
    """
    Change playback speed without pitch shift.
    Works best in range 0.5x – 2.5x.
    """
    altered = sound._spawn(
        sound.raw_data,
        overrides={"frame_rate": int(sound.frame_rate * speed)}
    )
    return altered.set_frame_rate(sound.frame_rate)


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
        # For KiriTTS, voice_id IS already the KiriTTS voice name (e.g. 'Chanda')
        # when called from cluster mode. For Edge-TTS fallback, map from Edge-TTS voice ID.
        if voice_id in KIRI_MALE_VOICES or voice_id in KIRI_FEMALE_VOICES:
            kiri_voice = voice_id  # Already a KiriTTS voice name from clustering
        else:
            kiri_voice = 'Chanda' if ('Piseth' in voice_id or 'Christopher' in voice_id or 'Yunxi' in voice_id) else 'Maly'
        
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
            res = requests.post(url, headers=headers, json=data)
            if res.status_code == 200:
                with open(out_path, 'wb') as f:
                    f.write(res.content)
                
                # Apply speed change if requested since KiriTTS API doesn't support rate
                speed_str = options.get('voice_speed', '1.0x').replace('x', '')
                try: speed_float = float(speed_str)
                except ValueError: speed_float = 1.0
                if speed_float != 1.0:
                    try:
                        clip = AudioSegment.from_file(out_path)
                        clip = speed_change(clip, speed_float)
                        clip.export(out_path, format="mp3")
                    except Exception as ex:
                        print(f"[KiriTTS Speed Error] {ex}")

                return  # Success!
            else:
                print(f"[KiriTTS Error] {res.status_code}: {res.text}. Falling back to Edge-TTS...")
        except Exception as e:
            print(f"[KiriTTS Request Failed] {e}. Falling back to Edge-TTS...")

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
    audio_path: str,
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
    sequential_mode: bool = False
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
        api_key = options.get('gemini_key') if options else None
        return detect_segment_gender(original_audio_path or audio_path, seg['start'], seg['end'], video_path=video_path, api_key=api_key)

    if genders is None:
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
        if kiri_voices is not None:
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

            if options and options.get('smart_emotion'):
                if text.endswith('!') or text.endswith('?!'):
                    clip = clip + 3
                    clip = speed_change(clip, 1.15)
                elif text.endswith('...') or text.endswith('…'):
                    clip = clip - 2
                    clip = speed_change(clip, 0.85)

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
            
            # Duck the background
            ducked = bg_layer[start_ms:end_ms] - 15
            bg_layer = bg_layer[:start_ms] + ducked + bg_layer[end_ms:]
            
            # Overlay speech on the separate speech layer to avoid ducking previous overlapping speech
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


def write_srt(segments, path: str):
    with open(path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, 1):
            f.write(
                f"{i}\n"
                f"{seconds_to_srt(seg['start'])} --> {seconds_to_srt(seg['end'])}\n"
                f"{seg['text'].strip()}\n\n"
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
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    
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
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            # Pass video file if available
            contents = [video_file, prompt] if video_file else [prompt]
            response = model.generate_content(contents)
            
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
    import google.generativeai as genai
    import json
    import time
    genai.configure(api_key=api_key)
    
    if upd_callback: upd_callback(job_id, message='☁️ Uploading video directly to Gemini…')
    video_file = genai.upload_file(path=video_path)
    
    if upd_callback: upd_callback(job_id, message='⏳ Waiting for Gemini to process video…')
    while video_file.state.name == 'PROCESSING':
        time.sleep(2)
        video_file = genai.get_file(video_file.name)
        
    if upd_callback: upd_callback(job_id, message='🧠 Gemini is native-transcribing and translating…')
    
    prompt = f"""You are a professional movie subtitle transcriber and translator.
Watch the provided video and transcribe every spoken word.
Translate all dialogue perfectly into {human_dest}.
Output your response as a RAW JSON array of objects (do NOT wrap it in markdown block like ```json).
Each object must have the following keys:
- "start": start time in seconds (float)
- "end": end time in seconds (float)
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
        model = genai.GenerativeModel('gemini-pro-latest')
        response = model.generate_content([video_file, prompt])
        
        raw_text = response.text.strip()
        start_idx = raw_text.find('[')
        end_idx = raw_text.rfind(']')
        
        if start_idx != -1 and end_idx != -1:
            json_str = raw_text[start_idx:end_idx+1]
        else:
            json_str = raw_text
            
        segments = json.loads(json_str)
    except Exception as e:
        print(f"Failed to parse Gemini JSON: {e}")
        segments = []
        
    try:
        genai.delete_file(video_file.name)
    except: pass
        
    return segments


# ─────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────
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
        transcriber_engine = options.get('transcriber', 'Whisper (Local)')
        gemini_key = options.get('gemini_key', '').strip()
        translator_engine = options.get('translator', 'Google Translate')
        
        if transcriber_engine == 'Gemini AI (Pro)' and gemini_key:
            # ── GEMINI NATIVE PIPELINE ──
            upd(job_id, stage='transcribe', progress=10, message='☁️ Initiating Gemini Native Transcription…')
            segments = gemini_transcribe_video(video_path, gemini_key, lang_label, upd_callback=upd, job_id=job_id)
            if not segments:
                raise Exception("Gemini failed to return valid JSON segments. Ensure your API key is correct and try again.")
            
            genders = []
            kiri_voices = []
            translated_segments = []
            
            for seg in segments:
                id_tag = seg.get('speaker_identity', '')
                translated_text = seg.get('text', '')
                if id_tag:
                    final_text = f"[{id_tag}]: {translated_text}"
                else:
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
            original_audio_path = video_path # For dubbing background
            if options.get('story_mode'):
                raise Exception("Storytelling mode requires Local Whisper to extract transcripts. Please change Transcriber to 'Whisper (Local)'.")
            
        else:
            # ── LOCAL WHISPER PIPELINE ──
            # ── 1. Extract audio ──────────────────────────────────
            debug_log("Calling upd for extract stage")
            upd(job_id, stage='extract', progress=8,
                message='🎵 Extracting audio from video…')
            
            debug_log("Starting subprocess ffmpeg extraction")
            subprocess.run(
                ['ffmpeg', '-i', video_path,
                 '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                 audio_path, '-y'],
                check=True, capture_output=True
            )
            debug_log("ffmpeg extraction completed successfully")

            original_audio_path = audio_path
            if options.get('remove_vocals'):
                upd(job_id, stage='isolate', progress=12, message='🎶 AI Vocal Removal: Isolating background music (this may take a minute)…')
                debug_log("Running demucs vocal separation")
                
                subprocess.run([
                    'demucs', '-n', 'htdemucs', '--two-stems', 'vocals', 
                    '-o', TEMP_DIR, '-d', 'cpu', audio_path
                ], check=True)
                
                base_name = os.path.splitext(os.path.basename(audio_path))[0]
                no_vocals_path = os.path.join(TEMP_DIR, 'htdemucs', base_name, 'no_vocals.wav')
                
                if os.path.exists(no_vocals_path):
                    original_audio_path = no_vocals_path
                    debug_log("Vocal removal succeeded.")
                else:
                    debug_log("Vocal removal failed to output file, falling back to original audio.")

            # ── 2. Transcribe using Whisper ───────────────────────
            speed_str = options.get('speed', 'Whisper Large (Perfect, Slow)')
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
            
            whisper_task = 'translate' if target_lang == 'English' else 'transcribe'
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

            if translator_engine == 'Gemini AI (Pro)' and gemini_key:
                upd(job_id, message='☁️ Uploading video to Gemini for visual context…')
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                try:
                    video_file = genai.upload_file(path=video_path)
                    upd(job_id, message='⏳ Waiting for Gemini to process video…')
                    while video_file.state.name == 'PROCESSING':
                        time.sleep(2)
                        video_file = genai.get_file(video_file.name)
                except Exception as e:
                    print(f"[Gemini Video Upload Error] {e}")
                    video_file = None

            translated_segments = []
            
            if options.get('story_mode'):
                upd(job_id, stage='translate', progress=48, message=f'📖 Writing AI Story in {lang_label}…')
                
                if not gemini_key:
                    raise Exception("Storytelling Mode requires a Gemini API Key. Please enter your API key in the 'Gemini/DeepSeek Key' text box.")
                    
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=gemini_key)
                    
                    transcript = "\n".join([f"[{s['start']:.1f}s - {s['end']:.1f}s]: {s['text']}" for s in segments])
                    prompt = (f"You are an expert movie narrator. Watch this video and read the following dialogue transcript. "
                              f"Write a continuous, engaging storytelling narrative that describes the visual actions and summarizes the conversations. "
                              f"Do not use timestamps or speaker tags. Write the entire story exclusively in {lang_label}. "
                              f"Make it flow naturally as a single story.\n\nTranscript:\n{transcript}")
                    
                    try:
                        model = genai.GenerativeModel('gemini-2.5-pro')
                        if video_file:
                            response = model.generate_content([prompt, video_file])
                        else:
                            response = model.generate_content(prompt)
                    except Exception as e:
                        if '429' in str(e) or 'quota' in str(e).lower():
                            print(f"[Gemini] Pro quota exceeded, falling back to Flash: {e}")
                            upd(job_id, message='⚠️ Pro quota exceeded. Falling back to Gemini Flash...')
                            model = genai.GenerativeModel('gemini-2.5-flash')
                            if video_file:
                                response = model.generate_content([prompt, video_file])
                            else:
                                response = model.generate_content(prompt)
                        else:
                            raise e
                            
                    story_text = response.text.strip()
                    import re
                    # Split into sentences for TTS
                    story_sentences = [s.strip() for s in re.split(r'(?<=[.!?។])\s+', story_text) if s.strip()]
                    for s in story_sentences:
                        translated_segments.append({'start': 0, 'end': 0, 'text': s})
                        genders = ['male'] * len(translated_segments) # Just use the male voice (single narrator)
                        
                except Exception as e:
                    print(f"Story generation failed: {e}")
                    raise Exception(f"Story generation failed: {str(e)}")
            elif target_lang == 'English' and whisper_task == 'translate':
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
                    
                    if translator_engine == 'Gemini AI (Pro)' and gemini_key:
                        translated_texts.extend(gemini_translate_batch(chunk, human_dest=lang_label, dest_code=dest_code, api_key=gemini_key, video_file=video_file))
                    elif translator_engine == 'DeepSeek API' and gemini_key:
                        translated_texts.extend(deepseek_translate_batch(chunk, human_dest=lang_label, dest_code=dest_code, api_key=gemini_key))
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
                
                if video_file:
                    try:
                        import google.generativeai as genai
                        genai.delete_file(video_file.name)
                    except:
                        pass

            translated_text = " ".join([s['text'] for s in translated_segments])

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
        timed_dub = build_timed_audio(
            translated_segments, audio_path, video_duration, job_id, tts_progress, 
            voice_male=options.get('voice_male') or voice_male, 
            voice_female=options.get('voice_female') or voice_female, 
            options=options, 
            original_audio_path=original_audio_path, 
            video_path=video_path,
            genders=['male'] * len(translated_segments) if is_story else None,
            sequential_mode=is_story
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
        style = f"Fontname={font_name},FontSize=10,PrimaryColour=&H00FFFF,Outline=1,Shadow=1"
        style_escaped = style.replace(",", "\\,")
        vf_filters.append(f"subtitles='{srt_path_ffmpeg}':fontsdir='{fonts_dir_ffmpeg}':force_style='{style_escaped}'")
        
        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path, '-i', timed_dub
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

class KhmerDubApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        global app_gui
        app_gui = self  
        self.title("KhmerDub - AI Video Translator")
        self.geometry("700x650")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.video_path = None
        
        # UI Layout
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(pady=20, padx=20, fill="both", expand=True)
        
        self.lbl_title = ctk.CTkLabel(self.main_frame, text="KhmerDub AI Translator", font=("Segoe UI", 24, "bold"))
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
        self.lbl_lang = ctk.CTkLabel(self.lang_frame, text="Dub Into:", font=("Segoe UI", 14, "bold"))
        self.lbl_lang.pack(side="left", padx=10)
        self.lang_var = ctk.StringVar(value="Khmer")
        self.opt_lang = ctk.CTkOptionMenu(self.lang_frame, variable=self.lang_var, values=["Khmer", "English", "Chinese"], font=("Segoe UI", 14), command=self.update_voice_defaults)
        self.opt_lang.pack(side="left", padx=5)
        
        self.lbl_speed = ctk.CTkLabel(self.lang_frame, text="Transcription:", font=("Segoe UI", 14, "bold"))
        self.lbl_speed.pack(side="left", padx=(20, 10))
        self.speed_var = ctk.StringVar(value="Whisper Large (Perfect, Slow)")
        self.opt_speed = ctk.CTkOptionMenu(self.lang_frame, variable=self.speed_var, values=["Whisper Large (Perfect, Slow)", "Whisper Medium (Great, Mod)", "Whisper Base (Okay, Fast)"], font=("Segoe UI", 14), width=180)
        self.opt_speed.pack(side="left", padx=5)
        
        self.trans_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.trans_frame.pack(pady=5, fill="x")
        self.lbl_trans = ctk.CTkLabel(self.trans_frame, text="Translator:", font=("Segoe UI", 14, "bold"))
        self.lbl_trans.pack(side="left", padx=10)
        self.trans_var = ctk.StringVar(value="Google Translate")
        self.opt_trans = ctk.CTkOptionMenu(self.trans_frame, variable=self.trans_var, values=["Google Translate", "Gemini AI (Pro)", "DeepSeek API"], font=("Segoe UI", 14), width=160)
        self.opt_trans.pack(side="left", padx=5)
        
        self.lbl_gemini_key = ctk.CTkLabel(self.trans_frame, text="Gemini/DeepSeek Key:", font=("Segoe UI", 14, "bold"))
        self.lbl_gemini_key.pack(side="left", padx=(10, 5))
        self.gemini_key_var = ctk.StringVar(value="")
        self.ent_gemini_key = ctk.CTkEntry(self.trans_frame, textvariable=self.gemini_key_var, placeholder_text="API Key...", show="*", width=150, font=("Segoe UI", 14))
        self.ent_gemini_key.pack(side="left", padx=5)
        
        self.engine_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.engine_frame.pack(pady=5, fill="x")
        self.lbl_engine = ctk.CTkLabel(self.engine_frame, text="Engine:", font=("Segoe UI", 14, "bold"))
        self.lbl_engine.pack(side="left", padx=10)
        self.engine_var = ctk.StringVar(value="Edge-TTS")
        self.opt_engine = ctk.CTkOptionMenu(self.engine_frame, variable=self.engine_var, values=["Edge-TTS", "KiriTTS"], font=("Segoe UI", 14), command=self.update_voice_defaults)
        self.opt_engine.pack(side="left", padx=5)
        
        self.lbl_transcriber = ctk.CTkLabel(self.engine_frame, text="Transcriber:", font=("Segoe UI", 14, "bold"))
        self.lbl_transcriber.pack(side="left", padx=(15, 5))
        self.transcriber_var = ctk.StringVar(value="Whisper (Local)")
        self.opt_transcriber = ctk.CTkOptionMenu(self.engine_frame, variable=self.transcriber_var, values=["Whisper (Local)", "Gemini AI (Pro)"], font=("Segoe UI", 14), width=140)
        self.opt_transcriber.pack(side="left", padx=5)

        self.lbl_voice_speed = ctk.CTkLabel(self.engine_frame, text="Voice Speed:", font=("Segoe UI", 14, "bold"))
        self.lbl_voice_speed.pack(side="left", padx=(15, 5))
        self.voice_speed_var = ctk.StringVar(value="1.0x")
        self.opt_voice_speed = ctk.CTkOptionMenu(self.engine_frame, variable=self.voice_speed_var, values=["1.0x", "1.25x", "1.5x", "1.75x", "2.0x"], font=("Segoe UI", 14), width=80)
        self.opt_voice_speed.pack(side="left", padx=5)
        
        self.lbl_key = ctk.CTkLabel(self.engine_frame, text="KiriTTS Key:", font=("Segoe UI", 14, "bold"))
        self.lbl_key.pack(side="left", padx=(20, 10))
        self.api_key_var = ctk.StringVar(value="")
        self.ent_key = ctk.CTkEntry(self.engine_frame, textvariable=self.api_key_var, placeholder_text="sk-...", show="*", width=200, font=("Segoe UI", 14))
        self.ent_key.pack(side="left", padx=5)
        
        self.voice_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.voice_frame.pack(pady=5, fill="x")
        
        self.lbl_male = ctk.CTkLabel(self.voice_frame, text="Male Model:", font=("Segoe UI", 14, "bold"))
        self.lbl_male.pack(side="left", padx=10)
        self.voice_male_var = ctk.StringVar(value="km-KH-PisethNeural")
        self.ent_male = ctk.CTkEntry(self.voice_frame, textvariable=self.voice_male_var, width=160, font=("Segoe UI", 14))
        self.ent_male.pack(side="left", padx=5)
        
        self.lbl_female = ctk.CTkLabel(self.voice_frame, text="Female Model:", font=("Segoe UI", 14, "bold"))
        self.lbl_female.pack(side="left", padx=(15, 5))
        self.voice_female_var = ctk.StringVar(value="km-KH-SreymomNeural")
        self.ent_female = ctk.CTkEntry(self.voice_frame, textvariable=self.voice_female_var, width=160, font=("Segoe UI", 14))
        self.ent_female.pack(side="left", padx=5)
        
        self.chk_mirror_var = ctk.StringVar(value="off")
        self.chk_blur_var = ctk.StringVar(value="off")
        self.chk_emotion_var = ctk.StringVar(value="on")
        self.chk_vocals_var = ctk.StringVar(value="off")
        self.chk_story_var = ctk.StringVar(value="off")
        
        self.chk_story = ctk.CTkCheckBox(self.main_frame, text="Enable Storytelling Mode (Narrates entire video with 1 voice)", variable=self.chk_story_var, onvalue="on", offvalue="off", fg_color="#ffc107", hover_color="#e0a800")
        self.chk_story.pack(pady=10)
        
        self.chk_vocals = ctk.CTkCheckBox(self.main_frame, text="Remove Original Vocals (Keep BGM/SFX)", variable=self.chk_vocals_var, onvalue="on", offvalue="off")
        self.chk_vocals.pack(pady=10)
        
        self.chk_mirror = ctk.CTkCheckBox(self.main_frame, text="Mirror Video (Avoid Copyright)", variable=self.chk_mirror_var, onvalue="on", offvalue="off")
        self.chk_mirror.pack(pady=15)
        
        self.chk_blur = ctk.CTkCheckBox(self.main_frame, text="Hide Watermarks (Cinematic Bars)", variable=self.chk_blur_var, onvalue="on", offvalue="off")
        self.chk_blur.pack(pady=10)
        
        self.btn_start = ctk.CTkButton(self.main_frame, text="Start Dubbing", command=self.start_dubbing, font=("Segoe UI", 18, "bold"), fg_color="#28a745", hover_color="#218838", height=45)
        self.btn_start.pack(pady=25)
        
        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)
        
        self.lbl_status = ctk.CTkLabel(self.main_frame, text="Ready", font=("Segoe UI", 14), text_color="#17a2b8")
        self.lbl_status.pack(pady=10)
        
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
                    'progress_hooks': [progress_hook],
                    'quiet': True,
                    'no_warnings': True,
                    'source_address': '0.0.0.0', 
                    'socket_timeout': 30,
                    'retries': 10,
                    'fragment_retries': 10
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
            
    def update_status(self, kw):
        if 'progress' in kw:
            self.progress_bar.set(kw['progress'] / 100.0)
        if 'message' in kw:
            self.lbl_status.configure(text=kw['message'])
        if kw.get('status') == 'complete':
            self.btn_start.configure(state="normal", text="Start Dubbing")
            out = kw.get('output_video')
            messagebox.showinfo("Success", f"Done! Video saved as:\n{out}")
            try:
                subprocess.Popen(f'explorer "{OUTPUT_DIR}"')
            except: pass
        elif kw.get('status') == 'error':
            self.btn_start.configure(state="normal", text="Start Dubbing")
            messagebox.showerror("Error", kw.get('message'))

    def update_voice_defaults(self, choice=None):
        engine = self.engine_var.get()
        lang = self.lang_var.get()
        if engine == "KiriTTS":
            self.voice_male_var.set("Rithy")
            self.voice_female_var.set("Maly")
        else:
            if lang == "Khmer":
                self.voice_male_var.set("km-KH-PisethNeural")
                self.voice_female_var.set("km-KH-SreymomNeural")
            elif lang == "English":
                self.voice_male_var.set("en-US-GuyNeural")
                self.voice_female_var.set("en-US-AriaNeural")
            elif lang == "Chinese":
                self.voice_male_var.set("zh-CN-YunxiNeural")
                self.voice_female_var.set("zh-CN-XiaoxiaoNeural")
            
    def start_dubbing(self):
        if not self.video_path:
            messagebox.showerror("Error", "Please select a video first!")
            return
            
        self.btn_start.configure(state="disabled", text="Processing...")
        self.progress_bar.set(0)
        self.lbl_status.configure(text="⚙️ Initializing AI components...")
        job_id = str(uuid.uuid4())[:8]
        options = {
            'mirror': self.chk_mirror_var.get() == "on",
            'blur': self.chk_blur_var.get() == "on",
            'smart_emotion': self.chk_emotion_var.get() == "on",
            'remove_vocals': self.chk_vocals_var.get() == "on",
            'target_lang': self.lang_var.get(),
            'transcriber': self.transcriber_var.get(),
            'translator': self.trans_var.get(),
            'speed': self.speed_var.get(),
            'voice_speed': self.voice_speed_var.get(),
            'tts_engine': self.engine_var.get(),
            'kiritts_key': self.api_key_var.get(),
            'gemini_key': self.gemini_key_var.get(),
            'voice_male': self.voice_male_var.get(),
            'voice_female': self.voice_female_var.get(),
            'story_mode': self.chk_story_var.get() == "on"
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
