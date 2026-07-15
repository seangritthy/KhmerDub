import sys
import yt_dlp

def download_wetv_video(url):
    """
    Downloads a video from WeTV using yt-dlp.
    """
    # Define yt-dlp options
    ydl_opts = {
        # Download best quality (you can adjust this if needed)
        'format': 'best',
        # Output filename template based on video title
        'outtmpl': '%(title)s.%(ext)s',
        # Display a custom progress bar
        'progress_hooks': [progress_hook],
        # Ignore warnings
        'quiet': False, 
        'no_warnings': True
    }

    print(f"Starting download for: {url}")
    print("-" * 50)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print("\n\nDownload completed successfully!")
    except Exception as e:
        print(f"\n\nAn error occurred during download: {e}")

def progress_hook(d):
    if d['status'] == 'downloading':
        percent = d.get('_percent_str', 'N/A').strip()
        speed = d.get('_speed_str', 'N/A').strip()
        eta = d.get('_eta_str', 'N/A').strip()
        # Overwrite the current line with progress
        sys.stdout.write(f"\rDownloading... {percent} | Speed: {speed} | ETA: {eta}")
        sys.stdout.flush()
    elif d['status'] == 'finished':
        print("\nDownload finished, finalizing file...")

if __name__ == "__main__":
    # Check if a URL was passed via command line
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        print("Welcome to the WeTV Downloader!")
        url = input("Please paste the WeTV video URL: ").strip()
        
    if url:
        download_wetv_video(url)
    
    # Pause so the console window doesn't immediately close
    input("\nPress Enter to exit...")
