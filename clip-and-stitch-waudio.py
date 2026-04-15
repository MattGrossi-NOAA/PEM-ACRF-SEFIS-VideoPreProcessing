"""
GoPro Clip-and-Stitch Utility
-----------------------------
A frame-accurate video processing tool designed for compiling survey videos
from the Southeast Fishery Independent Survey (SEFIS). This script automates
the extraction and concatenation of specific video segments from GoPro camera
folders based on "time-on-bottom" timestamps provided in a CSV file. It
ensures seamless stitching of video segments with precise millisecond
alignment, even across GoPro chapter seams, while also handling NTSC timing.
It works identical to `clip-and-stitch.py` but includes the audio tracks in
the new video.

Key Features:
    * Frame-Accurate Seeking: Uses FFmpeg's `trim` and `setpts` filters to 
        ensure exact millisecond alignment across GoPro chapter seams.
    * NTSC Correction: Automatically handles 29.97 fps (30000/1001) 
        timing to prevent timecode drift in long deployments.
    * Diagnostic Overlays: Provides optional burned-in timecode with 
        `HH:MM:SS:FF` format for frame-by-frame verification.
    * GoPro Logic: Intelligently sorts chapters (GX01, GX02) to maintain 
        chronological continuity.

Usage:
    python clip-and-stitch-waudio.py path/to/name-of-configuration-file.yml

Required Dependencies:
    * pandas: For CSV data management.
    * yaml: For configuration parsing.
    * tqdm: For progress visualization.
    * FFmpeg/ffprobe: Must be installed and accessible via system path 
        or config file.

Author:  matt.grossi@noaa.gov with creation and refactoring assistance from
         Google Gemini Coding Partner
Project: Southeast Fishery Independent Survey (SEFIS)
Version: 2026.0.1
Note:    Gemini Coding Partner was used to assist with developing this code. The
         code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

from datetime import datetime
import pandas as pd
import shutil
import yaml
import json
import os
import re
import sys
import time
import argparse
import subprocess
from tqdm import tqdm

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GoPro Clip-and-Stitch Utility")
    parser.add_argument(
        "config_path",
        type=str,
        nargs="?",
        default="configurations.yml",
        help="Path to the YAML configuration file (default: configurations.yml)"
    )
    return parser.parse_args()

def load_config(config_path='configurations.yml'):
    """Loads the YAML configuration file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def get_video_metadata(file_path, ffprobe_path):
    """Uses ffprobe to get internal creation time and duration of a video file.
    
    Arguments
    ---------
    file_path: str, path to the video file from which to extract metadata
    ffprobe_path: str, path to the `ffprobe` executable file`

    Returns
    -------
    list: [duration, creation_time, fps]
        duration: float, duration of the video in seconds
        creation_time: str, creation time of the video
            (e.g., "2024-01-01T12:00:00Z")
        fps: float, frames per second of the video
    """
    # ffprobe Command
    #   -v: verbose level (quiet suppresses output)
    #   -print_format: output format (json for easy parsing)
    #   -show_format: show container format info (includes duration)
    #   -show_streams: show stream info (video, audio, etc.)
    # See https://ffmpeg.org/ffmpeg.html
    cmd = [
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    
    # Extract duration and creation time
    duration = float(data['format']['duration'])
    # GoPro creation_time is usually in format tags
    creation_time = data['format'].get('tags', {})\
                                  .get('creation_time', '1970-01-01T00:00:00Z')
    
    # Extract FPS from the video stream metadata
    fps = get_fps_from_metadata(metadata=data)

    return duration, creation_time, fps

def get_fps_from_metadata(metadata):
    """
    Parses the ffprobe JSON to find the video stream's average frame rate.
    """
    # Load the JSON if it's a string, or use the dict directly
    data = json.loads(metadata) if isinstance(metadata, str) else metadata
    
    for stream in data.get('streams', []):
        # Look specifically for the video stream
        if stream.get('codec_type') == 'video':
            fps_str = stream.get('avg_frame_rate')
            
            # avg_frame_rate is usually a fraction string like "30000/1001"
            if '/' in fps_str:
                num, den = map(int, fps_str.split('/'))
                return num / den
            return float(fps_str)
            
    return 29.97  # Fallback if no video stream is found

def get_ffmpeg_command(config, tool="ffmpeg"):
    """
    Finds ffmpeg or ffprobe. Checks local folder, then config, then system.
    Compatible with Windows (.exe) and Mac/Linux (no extension).
    """
    # Detect the file extension based on the OS
    extension = ".exe" if sys.platform.startswith("win") else ""
    executable_name = f"{tool}{extension}"

    # 1. Check local folder created by setup
    local_path = os.path.join(os.getcwd(), "ffmpeg", "bin", executable_name)
    if os.path.exists(local_path):
        return local_path
        
    # 2. Fallback to config file
    config_key = f"{tool}_path"
    config_val = config.get(config_key)
    if config_val and os.path.exists(config_val):
        return config_val
        
    # 3. Last resort: assume it is in the system PATH
    return tool

def timestamp_to_seconds(ts_str, fps):
    """Converts HH:MM:SS:FF (frames) to total seconds (float)."""
    parts = ts_str.split(':')
    h, m, s, f = map(int, parts)
    return h * 3600 + m * 60 + s + (f / fps)

def seconds_to_timestamp(seconds, fps):
    """Converts total seconds (float) back to HH:MM:SS:FF format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    # Convert the decimal remainder of the second into frame units
    f = int(round((seconds % 1) * fps))
    
    # If rounding pushes frames to 30, roll it over to the next second
    if f >= int(round(fps)):
        f = 0
        s += 1
        # (Add logic here to roll over minutes/hours if needed for absolute perfection)
        
    return f"{h:02}:{m:02}:{s:02}:{f:02}"

def get_gopro_sort_key(filename):
    """
    Parses GoPro filenames (e.g., GX010192.MP4) for correct sorting.
    Returns a tuple: (Recording ID, Chapter Number)
    Example: GX020192 -> (0192, 02)
    """
    match = re.search(r'([A-Z]{2})(\d{2})(\d{4})', filename.upper())
    if match:
        prefix, chapter, rec_id = match.groups()
        return (int(rec_id), int(chapter))
    return (0, 0)

def process_deployments(config_path='configurations.yml'):
    # Strip quotes that Windows adds when dragging/dropping files
    config_path = config_path.strip('"')
    
    # 1. SETUP & VALIDATION
    config = load_config(config_path)
    os.makedirs(config['output_directory'], exist_ok=True)
    
    # Get FFmpeg paths
    ffmpeg_exe = get_ffmpeg_command(config, "ffmpeg")
    ffprobe_exe = get_ffmpeg_command(config, "ffprobe")

    # SETTINGS THAT CAN ALSO BE OVERRIDDEN IN THE YAML CONFIG FILE
    # Set defaults
    config['video_extension'] = config.get('video_extension', '.MP4')
    config['log_file'] = config.get('log_file', 'processing_log.txt')
    config['clear_log'] = config.get('clear_log', False)
    config['reprocess'] = config.get('reprocess', False)
    config['diagnostic_mode'] = config.get('diagnostic_mode', False)
    config['use_gpu'] = config.get('use_gpu', False)
    config['timeout_min'] = config.get('timeout_min', None)
    
    # Bug Fix: Ensure timeout multiplication only happens if value is not None
    timeout = config['timeout_min'] * 60 if config.get('timeout_min') else None

    # Video quality setting (18 is high quality, 23 is standard)
    # Default is 10 which, based on trial and error, produces bit rate and file
    # size most similar to those of the original GoPro files when `use_gpu` is
    # False.
    config['quality_crf'] = config.get('quality_crf', 10)
    
    # Minimum disk space required to run script (in GB). Script will warn if
    # available space is below this threshold.
    config['min_gb_required'] = config.get('min_gb_required', 10)

    # Check Disk Space
    total, used, free = shutil.disk_usage(config['output_directory'])
    if free // (2**30) < config['min_gb_required']:
        print(f"WARNING: Low disk space ({free // (2**30)}GB remaining).")

    # Initialize the session in the log file, wiping it clean if desired.
    mode = "w" if config['clear_log'] else "a"
    with open(config['log_file'], mode) as log:
        log.write(f"{'#'*80}\n")
        log.write(f"SESSION START: {datetime.now()}\n")
        log.write(f"CONFIGURATION: {config_path}\n\n")
        for key, value in config.items():
            log.write(f"  -> {key}: {value}\n")
        log.write("\n")
        log.write(f"{'#'*80}\n\n")

    # Load CSV with encoding fallback
    try:
        df = pd.read_csv(config['csv_path'], encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(config['csv_path'], encoding='ISO-8859-1')
    if len(df) == 0:
        print("ERROR: CSV file appears to be empty.")
        return

    # Clean CSV: Strip whitespace from headers and folder names
    df.columns = df.columns.str.strip()

    # Check for duplicates in foldername
    if df[config['col_foldername']].duplicated().any():
        print("ERROR: Duplicate folder names found in CSV. Aborting.")
        return

    # 2. ITERATE THROUGH EACH VIDEO FOLDER
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Total Progress"):
        # Start timer for this video
        iter_start = time.perf_counter()

        folder_id = str(row[config['col_foldername']]).strip()
        time_bottom_str = str(row[config['col_timebottom']]).strip()

        # Input directory
        folder_path = os.path.join(config['input_directory'], folder_id)
        if not os.path.exists(folder_path):
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: Folder {folder_path} not found.\n")
            continue

        output_path = os.path.join(config['output_directory'],
                                   f"{folder_id}{config['video_extension']}")

        # Skip if output exists and reprocess is false
        if not config['reprocess'] and os.path.exists(output_path):
            with open(config['log_file'], "a") as log:
                log.write(f"{os.path.basename(output_path)} exists and reprocess is set to False. Nothing to process. Skipping.\n")