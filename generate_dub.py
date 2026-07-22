import os
import sys
import uuid
import argparse

# We import the core logic from app.py to avoid code duplication
# Since app.py contains GUI code, we need to mock out the GUI updater if it expects app_gui to exist.
import app as khmerdub_app

def generate_video(video_path, kiritts_key, kiritts_profile="km-KH-NarongNeural", 
                   transcriber="Whisper Local (Free)", transcriber_key="",
                   translator="Google Translate (Free)", translator_key="",
                   voice_gender="Male", custom_prompt="", remove_vocals=False, hide_watermark=False):
    
    if not os.path.exists(video_path):
        print(f"Error: Video file not found at {video_path}")
        return

    print("==================================================")
    print(f"Starting CLI Dubbing Process for: {video_path}")
    print(f"Using Voice Engine: KiriTTS (Profile: {kiritts_profile})")
    print("==================================================")

    # Setup the options dictionary expected by the backend
    options = {
        'target_lang': 'Khmer',
        'transcriber': transcriber,
        'transcriber_key': transcriber_key,
        'translator': translator,
        'translator_key': translator_key,
        'voice_engine': 'KiriTTS (Khmer only)',
        'voice_gender': voice_gender,
        'kiritts_key': kiritts_key,
        'kiritts_profile': kiritts_profile,
        'custom_prompt': custom_prompt,
        'remove_vocals': remove_vocals,
        'blur': hide_watermark,
        'smart_emotion': False,
        'bgm_file': None,
        'bgm_volume': 1.0,
        'voice_speed_factor': 1.0
    }

    job_id = str(uuid.uuid4())[:8]

    # Mock the app_gui so progress updates don't crash if they try to access UI elements
    class MockGUI:
        class MockCancelEvent:
            def is_set(self): return False
        cancel_event = MockCancelEvent()
        
        def update_progress(self, *args, **kwargs):
            # Print the progress to terminal instead of UI
            msg = kwargs.get('message', args[2] if len(args) > 2 else '')
            progress = kwargs.get('progress', args[1] if len(args) > 1 else 0)
            if msg:
                print(f"[{progress:03}%] {msg}")
                
    khmerdub_app.app_gui = MockGUI()

    try:
        # Run the massive process_video function
        khmerdub_app.process_video(job_id, video_path, options)
        print("==================================================")
        print("? SUCCESS: Dubbing complete! Check your output folder.")
        print("==================================================")
    except Exception as e:
        print(f"\n? ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KhmerDub CLI - Generate dubbed video using KiriTTS")
    parser.add_argument("video_path", help="Path to the source video file (.mp4, .mkv, etc)")
    parser.add_argument("--kiri-key", required=True, help="API Key for KiriTTS")
    parser.add_argument("--kiri-profile", default="km-KH-NarongNeural", help="KiriTTS Voice Profile (e.g. km-KH-NarongNeural)")
    parser.add_argument("--gender", default="Male", choices=["Male", "Female", "Both (Smart)"], help="Target voice gender")
    parser.add_argument("--transcriber", default="Whisper Local (Free)", help="Transcriber engine to use")
    parser.add_argument("--translator", default="Google Translate (Free)", help="Translator engine to use")
    parser.add_argument("--transcriber-key", default="", help="API key for transcriber (if using Gemini/OpenAI)")
    parser.add_argument("--translator-key", default="", help="API key for translator (if using Gemini/DeepSeek)")
    parser.add_argument("--custom-prompt", default="", help="Custom style instructions for the translation AI")
    parser.add_argument("--remove-vocals", action="store_true", help="Remove original vocals using Demucs")
    parser.add_argument("--hide-watermark", action="store_true", help="Attempt to blur watermarks")

    args = parser.parse_args()
    
    generate_video(
        video_path=args.video_path,
        kiritts_key=args.kiri_key,
        kiritts_profile=args.kiri_profile,
        transcriber=args.transcriber,
        transcriber_key=args.transcriber_key,
        translator=args.translator,
        translator_key=args.translator_key,
        voice_gender=args.gender,
        custom_prompt=args.custom_prompt,
        remove_vocals=args.remove_vocals,
        hide_watermark=args.hide_watermark
    )
