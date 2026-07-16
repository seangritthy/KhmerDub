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
async def _generate_segment_tts_async(text: str, voice_id: str, out_path: str):
    # Natively generate at 1.5x speed (+50%) to avoid pitch distortion
    comm = edge_tts.Communicate(text, voice_id, rate="+50%")
    await comm.save(out_path)


def generate_segment_tts(text: str, voice_id: str, out_path: str):
    asyncio.run(_generate_segment_tts_async(text, voice_id, out_path))


# ─────────────────────────────────────────────────────────────
#  BUILD TIMED DUB TRACK  (per-segment gender detection)
# ─────────────────────────────────────────────────────────────
def build_timed_audio(
    segments: list,
    audio_path: str,          # original extracted audio for gender analysis
    video_duration: float,
    job_id: str,
    progress_callback=None,
    voice_male: str = 'km-KH-PisethNeural',
    voice_female: str = 'km-KH-SreymomNeural'
) -> str:
    """
    For each subtitle segment:
      1. Detect gender from that time slice of audio
      2. Choose male or female voice for target language
      3. Generate TTS audio with correct voice
      4. Overlay at exact start timestamp (NO speed adjustments)
    Returns path to final mixed WAV.
    """
    # Load original audio to keep background music. We will dynamically lower volume during TTS.
    try:
        master = AudioSegment.from_file(audio_path)
        # Pad duration just in case TTS audio extends a bit past the end
        target_dur_ms = int(video_duration * 1000) + 10000
        if len(master) < target_dur_ms:
            master = master + AudioSegment.silent(duration=target_dur_ms - len(master))
    except Exception as e:
        print(f"[Audio Load Error] {e}")
        master = AudioSegment.silent(duration=int(video_duration * 1000) + 10000)

    n = len(segments)
    for i, seg in enumerate(segments):
        text = seg['text'].strip()
        if not text:
            seg['gender'] = 'unknown'
            continue

        start_sec = seg['start']
        end_sec   = seg['end']
        start_ms  = int(start_sec * 1000)

        # ── Per-segment gender detection ──────────────────────
        gender   = detect_segment_gender(audio_path, start_sec, end_sec)
        voice_id = voice_male if gender == 'male' else voice_female
        seg['gender'] = gender      # store for UI display

        seg_tts_path = os.path.join(TEMP_DIR, f'{job_id}_seg{i}.mp3')
        try:
            generate_segment_tts(text, voice_id, seg_tts_path)
            clip = AudioSegment.from_file(seg_tts_path)

            # Smart Ducking: Lower original audio by 15dB ONLY during this specific TTS clip
            clip_len = len(clip)
            end_ms = start_ms + clip_len
            
            # Extract the original background chunk for this duration
            bg_chunk = master[start_ms:end_ms]
            # Lower its volume significantly
            bg_chunk = bg_chunk - 15
            # Overlay the loud Khmer TTS on top of the quieted background chunk
            bg_chunk = bg_chunk.overlay(clip)
            
            # Splice the processed chunk back into the master track
            master = master[:start_ms] + bg_chunk + master[end_ms:]

        except Exception as e:
            print(f'[TTS seg {i} {gender}] Error: {e}')
        finally:
            if os.path.exists(seg_tts_path):
                try: os.remove(seg_tts_path)
                except: pass

        if progress_callback:
            progress_callback(int((i+1)/n*100), i+1, n, gender)

    # Export master track
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
    translator = GoogleTranslator(source='auto', target=dest)
    for attempt in range(retries):
        try:
            res = translator.translate(text)
            if res:
                if "Error 500 (Server Error)" in res or "That's an error" in res:
                    raise Exception(f"Google returned a 500 error page instead of translating: {res[:50]}")
                return res
        except Exception as e:
            if attempt == retries - 1:
                print(f"[Translate Error] Final attempt failed for text '{text[:20]}...': {e}")
                return text
            # Re-initialize in case the session got blocked
            translator = GoogleTranslator(source='auto', target=dest)
            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s, 8s...
    return text


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

        # Translate per segment (robustly)
        translated_segments = []
        n_segs = len(segments)
        for i, seg in enumerate(segments):
            if target_lang == 'English' and whisper_task == 'translate':
                # Whisper already translated it to English perfectly!
                t = seg['text'].strip()
            else:
                t = robust_translate(seg['text'].strip(), dest=dest_code) or seg['text']
                
            translated_segments.append({
                'start': seg['start'],
                'end':   seg['end'],
                'text':  t,
            })
            if i % 5 == 0 or i == n_segs - 1:
                upd(job_id, message=f'🌏 Translating segments… ({i+1}/{n_segs})')
            # Add small delay between requests to avoid getting IP blocked
            time.sleep(0.3)

        # Translate full text for display by joining segments (avoids 5000 char limit)
        translated_text = " ".join([s['text'] for s in translated_segments])

        upd(job_id,
            translated_text=translated_text,
            segments=translated_segments,
            message=f'✅ {len(translated_segments)} segments translated to {lang_label}')
        time.sleep(0.3)

        # ── 5. Timed TTS — per-segment gender detection ───────
        upd(job_id, stage='tts', progress=60,
            message=f'🔊 Generating timed dubbing — detecting gender per line (natural speed)…')

        video_duration = get_video_duration(video_path)

        def tts_progress(pct, done, total, seg_gender='?'):
            icon = '👨' if seg_gender == 'male' else '👩'
            upd(job_id,
                progress=60 + int(pct * 0.22),
                message=f'🔊 {icon} {seg_gender.capitalize()} → {gender_to_label(seg_gender)} — line {done}/{total}…')

        timed_dub = build_timed_audio(
            translated_segments, audio_path, video_duration, job_id, tts_progress, voice_male, voice_female
        )
        # Update segments with gender info for UI
        upd(job_id, segments=translated_segments, progress=83,
            message=f'✅ Timed {lang_label} audio track ready')
        time.sleep(0.3)

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
        self.lbl_status.configure(text="Starting pipeline...")
        job_id = str(uuid.uuid4())[:8]
        options = {
            'mirror': self.chk_mirror_var.get() == "on",
            'blur': self.chk_blur_var.get() == "on",
            'target_lang': self.lang_var.get(),
            'speed': self.speed_var.get()
        }
        threading.Thread(target=process_video, args=(job_id, self.video_path, options), daemon=True).start()

# ── Standalone run ────────────────────────────────────────────
if __name__ == '__main__':
    app_gui = KhmerDubApp()
    app_gui.mainloop()
