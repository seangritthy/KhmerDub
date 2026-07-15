# KhmerDub 🇰🇭🎬

**KhmerDub** is an automated, AI-powered video localization tool that seamlessly dubs English (or any language) videos into natural-sounding Khmer (Cambodian). It automatically extracts audio, transcribes it, translates it to Khmer, generates perfectly lip-synced Khmer text-to-speech, and burns subtitles using the beautiful native Battambang font.

Built completely in Python with a standalone CustomTkinter Native Windows GUI.

## Features
- **Smart Voice Detection:** Automatically scans the audio to determine the speaker's gender and maps it to appropriate Khmer voices (e.g., Sreymom, Piseth).
- **Radio DJ Auto-Ducking:** Dynamically lowers the original video volume by 15dB precisely when the Khmer dubbed voice is speaking, maintaining background music and sound effects natively.
- **Auto-Sync:** Adjusts the tempo of the generated Khmer speech to perfectly fit within the original speaker's time window.
- **Cinematic Subtitles:** Automatically generates and burns `.srt` subtitles using the native `Battambang` font, designed specifically for mobile and desktop screens.
- **Mirroring & Watermark Removal:** Avoid copyright strikes and clean up videos by dynamically mirroring the video or applying cinematic black bars.

## Tech Stack
- **Whisper AI**: Speech-to-Text Transcription (Fast model)
- **Deep Translator**: Robust translation to Khmer with exponential backoff handling for API limits
- **Edge TTS**: High-quality Khmer text-to-speech engine
- **Pydub & Librosa**: Audio processing, gender detection, and tempo adjustment
- **CustomTkinter**: Native Windows GUI interface
- **FFmpeg**: Video, Audio, and Subtitle processing

## Installation
If you are running the compiled Native App (`KhmerDub.exe`), no installation is necessary! Simply run the executable.

### To Run from Source:
1. Ensure you have Python 3.11+ installed.
2. Clone this repository.
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Download the [FFmpeg](https://ffmpeg.org/) binaries and place `ffmpeg.exe` and `ffprobe.exe` inside an `ffmpeg_bin/` directory in the project root.
5. Run the app:
   ```bash
   python app.py
   ```

## Compiling
To compile KhmerDub into a single standalone `.exe` using PyInstaller:
```bash
pyinstaller KhmerDub.spec --clean
```
The resulting executable will be placed in the `dist/` directory.

## License
MIT License
