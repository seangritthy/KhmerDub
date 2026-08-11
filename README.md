# KhmerDub APK 🇰🇭🎬

**KhmerDub APK** is an automated, AI-powered video localization tool and mobile application that seamlessly dubs English (or foreign language) videos into natural-sounding Khmer (Cambodian). It automatically extracts audio, transcribes it, translates it to Khmer, generates natural Khmer text-to-speech, and burns soft/hard subtitles using the native `Battambang-Regular.ttf` font.

This repository contains both the **Native Desktop Application (Python / CustomTkinter)** and the **Native Android Webview APK Application** built directly for Termux & Android devices.

---

## 🌟 Key Features

- **Smart Voice Detection:** Scans audio to identify speaker gender and maps it to natural Khmer TTS voices (`km-KH-PisethNeural`, `km-KH-SreymomNeural`).
- **Audio Ducking:** Dynamically lowers background audio by ~15dB during Khmer speech to preserve background music and SFX.
- **Auto Sync & Tempo Adjustment:** Synchronizes Khmer speech tempo to match original time windows.
- **Khmer Subtitle Rendering:** Burns crisp Khmer subtitles using the native `Battambang-Regular.ttf` font.
- **Mobile Android Webview Interface:** Fast, lightweight, and modern UI engineered for phones and tablets.
- **Termux Command-Line APK Compiler:** Build signed release APKs directly on your Termux Android device without Android Studio!

---

## 🛠️ Tech Stack

- **Android Webview / Native Java**: Mobile app wrapper with local asset server
- **Whisper AI**: Fast speech-to-text transcription
- **Deep Translator**: Robust translation to Khmer with rate-limiting handling
- **Edge TTS**: High-fidelity Khmer text-to-speech engine
- **Pydub & Librosa**: Audio processing and gender classification
- **FFmpeg**: Video/audio encoding and subtitle burning
- **AAPT & d8 / apksigner**: Native Termux Android compilation stack

---

## 📱 Building the Android APK on Termux

You can compile the Android APK directly on Termux without needing a PC or Android Studio.

### Prerequisites:
Make sure standard Termux build tools are installed (`aapt`, `ecj` or `openjdk`, `d8`, `apksigner`, `zipalign`).

### Build Command:
Run the build script:
```bash
./build_apk.sh
```

This will automatically:
1. Fetch `android.jar` platform definitions.
2. Package Android resources via `aapt`.
3. Compile Java source code in `src/`.
4. Convert bytecode to `classes.dex` using `d8`.
5. Package, zip-align, and sign `khmerdubapk.apk` using `apksigner`.

Output path: `khmerdubapk.apk` (and `khmerdubapk-v1.0.1.apk`)

---

## 📦 Releases & Versioning

All development, builds, and official release packages are managed and published directly within this repository (`khmerdubapk`). Releases are not published to external APK repositories.

---

## 🖥️ Running the PC Desktop App

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Download `ffmpeg` binaries to `ffmpeg_bin/`.
3. Launch the desktop GUI:
   ```bash
   python app.py
   ```

---

## 📜 License
MIT License - Open source for community video localization in Cambodia 🇰🇭.
