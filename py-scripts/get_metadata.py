import subprocess
import argparse
import json

def parse_arguments():
    parser = argparse.ArgumentParser(description='Process video metadata.')
    parser.add_argument('file', type=str, help='Path to the video file')
    parser.add_argument('--ffprobe_path', type=str, default='ffmpeg/bin/ffprobe.exe',
                        help='Path to the ffprobe executable (default: ffmpeg/bin/ffprobe.exe)')
    return parser.parse_args()

def main():
    """Uses ffprobe to display internal metadata of a video file.
    
    Arguments
    ---------
    file_path: str, path to the video file from which to extract metadata
    ffprobe_path: str, path to the `ffprobe` executable file`

    Returns
    -------
    Prints metadata of the video file to the console in JSON format
    """
    # Parse arguments
    args = parse_arguments()

    # ffprobe Command
    #   -v: verbose level (quiet suppresses output)
    #   -print_format: output format (json for easy parsing)
    #   -show_format: show container format info (includes duration)
    #   -show_streams: show stream info (video, audio, etc.)
    # See https://ffmpeg.org/ffmpeg.html
    cmd = [
        args.ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", args.file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return data

if __name__ == "__main__":
    metadata = main()
    print(json.dumps(metadata, indent=4))