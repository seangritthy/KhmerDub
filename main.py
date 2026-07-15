"""
KhmerDub — Desktop App Entry Point
Uses pywebview for native window. Exposes a JS API for
native save-file dialogs so downloads work without a browser.
"""
import sys
import os
import threading
import time
import ctypes
import shutil

# ── Resolve paths ────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    APP_DATA = os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')), 'KhmerDub'
    )
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DATA = BASE_DIR

for _d in ['uploads', 'outputs', 'temp']:
    os.makedirs(os.path.join(APP_DATA, _d), exist_ok=True)

OUTPUT_DIR = os.path.join(APP_DATA, 'outputs')

# ── Bundle FFmpeg into PATH ──────────────────────────────────
_ffmpeg_bin = os.path.join(BASE_DIR, 'ffmpeg_bin')
if os.path.isdir(_ffmpeg_bin):
    os.environ['PATH'] = _ffmpeg_bin + os.pathsep + os.environ.get('PATH', '')

STATIC_DIR = os.path.join(BASE_DIR, 'static')
sys.path.insert(0, BASE_DIR)

from app import create_app   # noqa: E402

PORT = 5000
URL  = f'http://127.0.0.1:{PORT}'


# ─────────────────────────────────────────────────────────────
#  PYWEBVIEW JS API  — called from JavaScript via
#  window.pywebview.api.<method>()
# ─────────────────────────────────────────────────────────────
class KhmerDubAPI:
    """Exposes Python functions to JavaScript running inside the webview."""

    def __init__(self):
        self._window = None   # set after webview window is created

    def _get_window(self):
        import webview
        return webview.windows[0] if webview.windows else None

    # ── Save file via native Windows save dialog ──────────────
    def save_file(self, filename: str) -> dict:
        """
        Open a native Save-As dialog and copy the output file
        to wherever the user chooses.
        Returns: {ok: bool, path: str, error: str}
        """
        src = os.path.join(OUTPUT_DIR, filename)
        if not os.path.isfile(src):
            return {'ok': False, 'error': f'File not found: {filename}'}

        ext  = os.path.splitext(filename)[1].lower()
        is_video = ext in ('.mp4', '.avi', '.mov', '.mkv')
        default_name = 'KhmerDub_dubbed.mp4' if is_video else 'KhmerDub_subtitles.srt'
        file_types   = ('MP4 Video (*.mp4)',) if is_video else ('SRT Subtitles (*.srt)',)

        win = self._get_window()
        if win is None:
            return {'ok': False, 'error': 'No window reference'}

        try:
            import webview
            result = win.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=os.path.expanduser('~\\Desktop'),
                save_filename=default_name,
                file_types=file_types,
            )
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        if not result:
            return {'ok': False, 'error': 'Cancelled'}

        dest = result if isinstance(result, str) else result[0]
        # Ensure correct extension
        if is_video and not dest.lower().endswith('.mp4'):
            dest += '.mp4'
        if not is_video and not dest.lower().endswith('.srt'):
            dest += '.srt'

        try:
            shutil.copy2(src, dest)
            return {'ok': True, 'path': dest}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── Open outputs folder in Windows Explorer ───────────────
    def open_folder(self) -> dict:
        try:
            os.startfile(OUTPUT_DIR)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── Get output folder path (for display) ─────────────────
    def get_output_dir(self) -> str:
        return OUTPUT_DIR


# ─────────────────────────────────────────────────────────────
#  FLASK STARTUP
# ─────────────────────────────────────────────────────────────
def start_flask(flask_app):
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    flask_app.run(host='127.0.0.1', port=PORT,
                  debug=False, threaded=True, use_reloader=False)


def wait_for_server(timeout=15):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    if sys.platform == 'win32':
        try:
            ctypes.windll.kernel32.SetConsoleTitleW('KhmerDub')
        except Exception:
            pass

    print("=" * 48)
    print("  KhmerDub — AI Video Auto-Dub to Khmer")
    print("=" * 48)
    print(f"  📁 Output folder : {OUTPUT_DIR}")
    print()

    flask_app = create_app(
        static_dir=STATIC_DIR,
        upload_dir=os.path.join(APP_DATA, 'uploads'),
        output_dir=OUTPUT_DIR,
        temp_dir=os.path.join(APP_DATA, 'temp'),
    )

    threading.Thread(target=start_flask, args=(flask_app,), daemon=True).start()

    print("  ⏳ Starting server...")
    if not wait_for_server():
        print("  ❌ Server failed to start.")
        sys.exit(1)
    print("  ✅ Ready — opening app window...\n")

    import webview

    api = KhmerDubAPI()

    window = webview.create_window(
        title='🇰🇭  KhmerDub — AI Video Dubber',
        url=URL,
        width=1140,
        height=820,
        resizable=True,
        min_size=(860, 620),
        background_color='#080b14',
        easy_drag=False,
        confirm_close=False,
        js_api=api,          # ← expose API to JavaScript
    )

    webview.start(
        debug=False,
        gui='edgechromium',
        http_server=False,
    )


if __name__ == '__main__':
    main()
