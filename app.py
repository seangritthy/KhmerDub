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

# ── Runtime directories ───────────
_BASE      = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_BASE, 'outputs')
TEMP_DIR   = os.path.join(_BASE, 'temp')

for _d in [OUTPUT_DIR, TEMP_DIR]:
    os.makedirs(_d, exist_ok=True)

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

def detect_segment_gender(audio_path: str, start_sec: float, end_sec: float) -> str:
    """Detect gender for a specific time slice of the audio."""
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

def cluster_speakers(audio_path: str, segments: list, genders: list) -> list:
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
            return _fallback_voices(genders)

        X = np.array([features[i] for i in valid_idx])
        X = StandardScaler().fit_transform(X)

        # Auto-select number of clusters (2–6 speakers max)
        n_clusters = min(max(2, len(valid_idx) // 5), 6)
        labels = AgglomerativeClustering(n_clusters=n_clusters).fit_predict(X)

        # Map cluster → voice (consistent per cluster, respecting gender)
        cluster_voice_map = {}
        male_pool   = list(KIRI_MALE_VOICES)
        female_pool = list(KIRI_FEMALE_VOICES)
        male_idx_count   = 0
        female_idx_count = 0

        for pos, seg_i in enumerate(valid_idx):
            cluster = labels[pos]
            if cluster not in cluster_voice_map:
                gender = genders[seg_i] or 'male'
                if gender == 'male':
                    cluster_voice_map[cluster] = male_pool[male_idx_count % len(male_pool)]
                    male_idx_count += 1
                else:
                    cluster_voice_map[cluster] = female_pool[female_idx_count % len(female_pool)]
                    female_idx_count += 1

        # Build final voice list aligned to all segments
        voices = []
        vi = 0
        for i, seg in enumerate(segments):
            if i in valid_idx:
                pos = valid_idx.index(i)
                voices.append(cluster_voice_map.get(labels[pos], 'Chanda'))
                vi += 1
            else:
                voices.append('Chanda')

        print(f"[Speaker Cluster] Detected {n_clusters} unique speakers → voices: {cluster_voice_map}")
        return voices

    except Exception as e:
        print(f"[Speaker Cluster Error] {e} — using fallback")
        return _fallback_voices(genders)


def _fallback_voices(genders: list) -> list:
    """Simple fallback: Chanda for male, Maly for female."""
    return ['Chanda' if (g or 'male') == 'male' else 'Maly' for g in genders]



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
async def _generate_segment_tts_async(text: str, voice_id: str, out_path: str, retries=4):
    # Natively generate at 1.5x speed (+50%) to avoid pitch distortion
    for attempt in range(retries):
        try:
            comm = edge_tts.Communicate(text, voice_id, rate="+50%")
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
                return  # Success!
            else:
                print(f"[KiriTTS Error] {res.status_code}: {res.text}. Falling back to Edge-TTS...")
        except Exception as e:
            print(f"[KiriTTS Request Failed] {e}. Falling back to Edge-TTS...")

    # Default/Fallback: Edge-TTS
    asyncio.run(_generate_segment_tts_async(text, voice_id, out_path))


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
    options: dict = None
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
        return detect_segment_gender(audio_path, seg['start'], seg['end'])

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
    # Assigns a unique voice to each distinct character in the video
    kiri_voices = None
    if options and options.get('tts_engine') == 'KiriTTS' and options.get('kiritts_key', '').strip():
        upd_msg = getattr(options, '_upd', None)  # won't work here, just print
        print('[Speaker Cluster] Running speaker clustering for KiriTTS...')
        kiri_voices = cluster_speakers(audio_path, segments, genders)
        print(f'[Speaker Cluster] Voice assignments: {kiri_voices}')

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

            start_ms = int(seg['start'] * 1000)
            clip_len  = len(clip)
            end_ms    = start_ms + clip_len
            bg_chunk  = master[start_ms:end_ms] - 15
            bg_chunk  = bg_chunk.overlay(clip)
            master    = master[:start_ms] + bg_chunk + master[end_ms:]
        except Exception as e:
            print(f'[Overlay seg {i}] Error: {e}')
        finally:
            try: os.remove(path)
            except: pass

    out_path = os.path.join(TEMP_DIR, f'{job_id}_timed_dub.wav')
    master.export(out_path, format='wav')
    return out_path


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def upd(job_id: str, **kw):
    if app_gui:
        # Schedule GUI update on the main thread safely
        app_gui.after(0, app_gui.update_status, kw)


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
    for attempt in range(retries):
        try:
            translator = GoogleTranslator(source='auto', target=dest)
            # translate_batch is much more efficient than looping
            results = translator.translate_batch(texts)
            return [r if r else t for r, t in zip(results, texts)]
        except Exception as e:
            if attempt == retries - 1:
                print(f"[Translate Batch Error] Final attempt failed: {e}")
                return texts
            time.sleep(2 ** attempt)
    return texts

# ─────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────
def process_video(job_id: str, video_path: str, options: dict):
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
        # ── 1. Extract audio ──────────────────────────────────
        upd(job_id, stage='extract', progress=8,
            message='🎵 Extracting audio from video…')
        subprocess.run(
            ['ffmpeg', '-i', video_path,
             '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
             audio_path, '-y'],
            check=True, capture_output=True
        )

        # ── 2. Overall gender scan (for UI badge) ────────────
        upd(job_id, stage='gender', progress=18,
            message='🔍 Scanning overall voice characteristics…')
        gender_overall = detect_voice_gender(audio_path)
        voice_lbl      = gender_to_label(gender_overall)
        upd(job_id, gender=gender_overall, voice=voice_lbl,
            message=f'✅ Dominant voice: {voice_lbl} — each line will be detected individually')
        time.sleep(0.6)

        # ── 3. Transcribe / Translate ─────────────────────────
        upd(job_id, stage='transcribe', progress=30,
            message='📝 Analyzing speech with Whisper AI…')
        model         = get_whisper_model(options.get('speed', 'High Quality (Slow)'))
        
        # If target is English, use Whisper's built-in Any-to-English translation task for maximum accuracy
        whisper_task  = 'translate' if target_lang == 'English' else 'transcribe'
        result        = model.transcribe(audio_path, task=whisper_task)
        
        segments      = result.get('segments', [])
        original_text = result.get('text', '').strip()
        detected_lang = result.get('language', 'unknown')
        upd(job_id,
            detected_language=detected_lang,
            original_text=original_text,
            message=f'✅ Transcribed ({detected_lang.upper()}) — {len(segments)} segments')
        time.sleep(0.3)

        # ── 4. Translate to Target Language ───────────────────
        upd(job_id, stage='translate', progress=48,
            message=f'🌏 Translating segments to {lang_label}…')

        translated_segments = []
        n_segs = len(segments)
        
        if target_lang == 'English' and whisper_task == 'translate':
            # Whisper already translated it to English perfectly!
            for seg in segments:
                translated_segments.append({
                    'start': seg['start'],
                    'end':   seg['end'],
                    'text':  seg['text'].strip()
                })
        else:
            # Batch translate all segments at once!
            texts_to_translate = [seg['text'].strip() for seg in segments]
            
            # Google Translate allows up to 5K chars, but translating 50 strings at once is perfectly safe
            # We split into chunks of 50 just to be safe
            translated_texts = []
            chunk_size = 50
            for i in range(0, len(texts_to_translate), chunk_size):
                chunk = texts_to_translate[i:i + chunk_size]
                upd(job_id, message=f'🌏 Translating batch {i//chunk_size + 1}…')
                translated_texts.extend(robust_translate_batch(chunk, dest=dest_code))
                if target_lang != 'English':
                    time.sleep(0.5) # small delay between batches
            
            for seg, t_text in zip(segments, translated_texts):
                translated_segments.append({
                    'start': seg['start'],
                    'end':   seg['end'],
                    'text':  t_text or seg['text'].strip(),
                })

        # Translate full text for display by joining segments
        translated_text = " ".join([s['text'] for s in translated_segments])

        upd(job_id,
            translated_text=translated_text,
            segments=translated_segments,
            message=f'✅ {len(translated_segments)} segments translated to {lang_label}')

        # ── 5. Timed TTS — parallel gender detection + TTS ───
        upd(job_id, stage='tts', progress=60,
            message=f'🔊 Generating timed dubbing in parallel…')

        video_duration = get_video_duration(video_path)

        def tts_progress(pct, done, total, seg_gender='?'):
            icon = '👨' if seg_gender == 'male' else '👩'
            upd(job_id,
                progress=60 + int(pct * 0.22),
                message=f'🔊 {icon} {seg_gender.capitalize()} → {gender_to_label(seg_gender)} — line {done}/{total}…')

        timed_dub = build_timed_audio(
            translated_segments, audio_path, video_duration, job_id, tts_progress, voice_male, voice_female, options=options
        )
        # Update segments with gender info for UI
        upd(job_id, segments=translated_segments, progress=83,
            message=f'✅ Timed {lang_label} audio track ready')

        # ── 5.5 Write SRT (needed for burning) ─────────────────
        srt_file = os.path.join(OUTPUT_DIR, f'{job_id}_khmer.srt')
        write_srt(translated_segments, srt_file)

        # ── 6. Merge timed audio with video and burn subtitles ─
        upd(job_id, stage='merge', progress=87,
            message='🎬 Merging timed dubbed audio with video and burning subtitles…')
        out_video = os.path.join(OUTPUT_DIR, f'{job_id}_khmer_dubbed.mp4')
        
        # Build FFmpeg filters based on user options
        vf_filters = []
        if options.get('mirror'):
            vf_filters.append('hflip')
        if options.get('blur'):
            # Add cinematic black bars (11%) to hide watermarks top/bottom
            vf_filters.append('drawbox=x=0:y=0:w=iw:h=ih/9:color=black@0.9:t=fill')
            vf_filters.append('drawbox=x=0:y=ih-ih/9:w=iw:h=ih/9:color=black@0.9:t=fill')

        # Add subtitle burning filter (escape Windows paths for FFmpeg)
        srt_path_ffmpeg = srt_file.replace('\\', '/').replace(':', '\\:')
        
        # Point FFmpeg to the directory containing Battambang-Regular.ttf
        fonts_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        fonts_dir_ffmpeg = fonts_dir.replace('\\', '/').replace(':', '\\:')

        # Commas inside force_style MUST be escaped with backslash, otherwise FFmpeg parses them as new filters
        style = f"Fontname={font_name},FontSize=10,PrimaryColour=&H00FFFF,Outline=1,Shadow=1"
        style_escaped = style.replace(",", "\\,")
        vf_filters.append(f"subtitles='{srt_path_ffmpeg}':fontsdir='{fonts_dir_ffmpeg}':force_style='{style_escaped}'")

        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path, '-i', timed_dub
        ]
        
        # Always re-encode because we are using -vf (subtitles)
        ffmpeg_cmd.extend(['-vf', ','.join(vf_filters)])
        ffmpeg_cmd.extend(['-c:v', 'libx264', '-preset', 'fast', '-crf', '24'])
            
        ffmpeg_cmd.extend([
            '-map', '0:v:0', '-map', '1:a:0',
            '-shortest', out_video, '-y'
        ])
        
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)

        upd(job_id,
            stage='complete', progress=100, status='complete',
            message='🎉 Done! Khmer dubbed video is ready (lip-sync timed).',
            output_video=f'{job_id}_khmer_dubbed.mp4',
            output_srt=f'{job_id}_khmer.srt')

    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b'').decode(errors='replace')[:400]
        upd(job_id, stage='error', status='error',
            message=f'❌ FFmpeg error: {err}')
    except Exception as exc:
        import traceback
        upd(job_id, stage='error', status='error',
            message=f'❌ Error: {exc}')
        print(traceback.format_exc())
    finally:
        for p in [audio_path, timed_dub]:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass


# ─────────────────────────────────────────────────────────────
#  NATIVE GUI (CustomTkinter)
# ─────────────────────────────────────────────────────────────
class KhmerDubApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        global app_gui
        app_gui = self  # Set immediately so pipeline can update UI
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
        
        # File selection frame
        self.file_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.file_frame.pack(pady=10, fill="x")
        
        self.btn_select = ctk.CTkButton(self.file_frame, text="Select Local Video", command=self.select_video, font=("Segoe UI", 14), width=180)
        self.btn_select.pack(side="left", padx=10)
        
        self.lbl_file = ctk.CTkLabel(self.file_frame, text="No video selected", font=("Segoe UI", 12), text_color="gray")
        self.lbl_file.pack(side="left", padx=10)
        
        # URL Download frame
        self.url_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.url_frame.pack(pady=5, fill="x")
        
        self.url_entry = ctk.CTkEntry(self.url_frame, placeholder_text="Or paste video URL here (WeTV, YouTube, etc.)", font=("Segoe UI", 12), width=350)
        self.url_entry.pack(side="left", padx=10)
        
        self.btn_download = ctk.CTkButton(self.url_frame, text="Download Video", command=self.start_download, font=("Segoe UI", 14), width=140, fg_color="#17a2b8", hover_color="#138496")
        self.btn_download.pack(side="left", padx=5)
        
        # Target language dropdown
        self.lang_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.lang_frame.pack(pady=5, fill="x")
        self.lbl_lang = ctk.CTkLabel(self.lang_frame, text="Dub Into:", font=("Segoe UI", 14, "bold"))
        self.lbl_lang.pack(side="left", padx=10)
        self.lang_var = ctk.StringVar(value="Khmer")
        self.opt_lang = ctk.CTkOptionMenu(self.lang_frame, variable=self.lang_var, values=["Khmer", "English", "Chinese"], font=("Segoe UI", 14))
        self.opt_lang.pack(side="left", padx=5)
        
        # Speed dropdown
        self.lbl_speed = ctk.CTkLabel(self.lang_frame, text="Speed:", font=("Segoe UI", 14, "bold"))
        self.lbl_speed.pack(side="left", padx=(20, 10))
        self.speed_var = ctk.StringVar(value="High Quality (Slow)")
        self.opt_speed = ctk.CTkOptionMenu(self.lang_frame, variable=self.speed_var, values=["High Quality (Slow)", "Fast (Less Accurate)"], font=("Segoe UI", 14))
        self.opt_speed.pack(side="left", padx=5)
        
        # TTS Engine dropdown and KiriTTS API Key
        self.engine_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.engine_frame.pack(pady=5, fill="x")
        self.lbl_engine = ctk.CTkLabel(self.engine_frame, text="Engine:", font=("Segoe UI", 14, "bold"))
        self.lbl_engine.pack(side="left", padx=10)
        self.engine_var = ctk.StringVar(value="Edge-TTS")
        self.opt_engine = ctk.CTkOptionMenu(self.engine_frame, variable=self.engine_var, values=["Edge-TTS", "KiriTTS"], font=("Segoe UI", 14))
        self.opt_engine.pack(side="left", padx=5)
        
        self.lbl_key = ctk.CTkLabel(self.engine_frame, text="KiriTTS Key:", font=("Segoe UI", 14, "bold"))
        self.lbl_key.pack(side="left", padx=(20, 10))
        self.api_key_var = ctk.StringVar(value="")
        self.ent_key = ctk.CTkEntry(self.engine_frame, textvariable=self.api_key_var, placeholder_text="sk-...", show="*", width=200, font=("Segoe UI", 14))
        self.ent_key.pack(side="left", padx=5)
        
        self.chk_mirror_var = ctk.StringVar(value="off")
        self.chk_blur_var = ctk.StringVar(value="off")
        
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
                            # Parse percentage from string like " 25.4%" or "\x1b[0;34m 25.4%\x1b[0m"
                            p = d.get('_percent_str', '0%').replace('%', '').strip()
                            import re
                            p = re.sub(r'\x1b\[[0-9;]*m', '', p)
                            pct = float(p)
                            self.after(0, lambda: self.progress_bar.set(pct / 100.0))
                            
                            # Parse speed
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
                    'source_address': '0.0.0.0', # Force IPv4 to prevent YouTube timeouts
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
                        
                    # yt-dlp sometimes changes the extension (e.g. to .mkv) after merging
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
                    # Automatically start dubbing!
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
            'target_lang': self.lang_var.get(),
            'speed': self.speed_var.get(),
            'tts_engine': self.engine_var.get(),
            'kiritts_key': self.api_key_var.get()
        }
        
        def run_with_error_catch():
            try:
                process_video(job_id, self.video_path, options)
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                print(err)
                self.after(0, lambda: [
                    self.btn_start.configure(state="normal", text="Start Dubbing"),
                    self.lbl_status.configure(text=f"❌ Error: {e}"),
                    messagebox.showerror("Pipeline Error", f"{e}\n\n{err[-500:]}")
                ])
        
        threading.Thread(target=run_with_error_catch, daemon=True).start()

# ── Standalone run ────────────────────────────────────────────
if __name__ == '__main__':
    app = KhmerDubApp()
    app.mainloop()
