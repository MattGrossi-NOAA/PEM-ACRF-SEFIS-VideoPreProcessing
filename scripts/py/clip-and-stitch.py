"""
SEFIS GoPro Clip-and-Stitch Utility
-----------------------------------
A frame-accurate video processing tool designed for compiling survey videos
from the Southeast Fishery Independent Survey (SEFIS). This script automates
the extraction and concatenation of specific video segments from GoPro camera
folders based on "start_time" timestamps provided in a CSV file. It ensures
seamless stitching of video segments with precise millisecond alignment across
GoPro chapter seams.

Key Features:
    * Parallel Processing: Scales across CPU/GPU workers for bulk processing.
    * Resilient Serial Upload: Pushes new video to a GCP cloud bucket using
        `gcloud storage` after each encode.
    * Diagnostic Overlays: Provides optional burned-in time code with 
        `HH:MM:SS:FF` format for frame-by-frame verification.

Usage:
    python clip-and-stitch.py path/to/name-of-configuration-file.yml --include-audio

Required Dependencies:
    * pandas: For CSV data management.
    * yaml: For configuration parsing.
    * tqdm: For progress visualization.
    * FFmpeg/ffprobe: Must be installed and accessible via system path 
        or config file.
    * Google Cloud Software Development Kit (SDK): Google Cloud command line
        interface (CLI) for pushing videos to cloud bucket

Author:  matt.grossi@noaa.gov with creation and refactoring assistance from
         Google Gemini Coding Partner
Project: Southeast Fishery Independent Survey (SEFIS)
Version: 2026.2.0
Note:    Gemini Coding Partner was used to assist with developing this code.
         The code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

# =============================================================================
# PACKAGE DEPENDENCIES
# =============================================================================

from collections import defaultdict
from datetime import datetime
from typing import Literal
import pandas as pd
import argparse
import shutil
import yaml
import json
import os
import re
import sys
import time
import difflib
import textwrap
import subprocess
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor

# =============================================================================
# HELPER FUNCTIONS AND METHODS
# =============================================================================

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GoPro Clip-and-Stitch Utility")
    parser.add_argument("config_path", type=str, nargs="?",
        default="configurations.yml",
        help="Path to the YAML configuration file (default: configurations.yml)"
    )
    parser.add_argument("--no-processing", action="store_false", dest='process',
        help="Carry out logging without conducting any video processing"
    )
    return parser.parse_args()

# Define a simple mock class to handle the --no-process case
class MockResult:
    returncode = 0
    stderr = "Execution suppressed by --no-process"

def clean_and_validate_config(config: dict):
    """Checks for missing mandatory keys and typos in the YAML. Suggests the
    closest-match valid key for any invalid key found. Cleans any string values
    when Bools are expected, ensures video file extension, if passed, contains
    a leading ".", and ensures the GCP bucket path, if passed, ends with a "/"
    to ensure it is treated as a file prefix.
    
    Arguments
    ---------
    config (dict): dictionary of configuration settings to validate
    """
    # Check for missing mandatory entries
    REQUIRED_KEYS = {'col_folder_name', 'col_start_time', 'csv_path',
                     'input_directory', 'output_directory'}
    missing = [f"  - '{k}'" for k in REQUIRED_KEYS if k not in config]
    
    if missing:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Missing mandatory settings in YAML:\n" +
            "\n".join(missing) +
            "\n\nThe pipeline cannot start without these core paths defined."
        )
        raise ValueError(error_msg)

    # Check for typos or unrecognized keys
    VALID_KEYS = {
        'clear_log', 'col_folder_name', 'col_start_time', 'csv_path',
        'delete_local_after_upload', 'diagnostic_mode',
        'ffmpeg_path', 'ffprobe_path',
        'gcp_bucket_path', 'gcp_upload',
        'include_audio', 'input_directory',
        'log_file',
        'max_retries', 'min_gb_required',
        'num_workers',
        'output_directory', 'output_fps',
        'quality_crf',
        'reprocess',
        'skip_partial_videos', 'start_time_fps',
        'time_buffer_minutes', 'timeout_minutes',
        'use_gpu',
        'video_duration_minutes', 'video_extension'
    }
    
    unrecognized = []
    for key in config:
        if key not in VALID_KEYS:
            matches = difflib.get_close_matches(key, list(VALID_KEYS), n=1, cutoff=0.6)
            suggestion = f" (Did you mean '{matches[0]}'?)" if matches else ""
            unrecognized.append(f"  - '{key}'{suggestion}")

    if unrecognized:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Unrecognized settings found in YAML:\n" +
            "\n".join(unrecognized) +
            "\n\nPlease correct your configuration file and restart the utility."
        )
        raise ValueError(error_msg)

    # Clean up Bool , if needed
    BOOLEAN_KEYS = {
        'clear_log', 'delete_local_after_upload', 'diagnostic_mode', 
        'gcp_upload', 'include_audio', 'reprocess', 'skip_partial_videos',
        'use_gpu'
    }
    for key in BOOLEAN_KEYS:
        if key in config and isinstance(config[key], str):
            clean_val = config[key].strip().lower()
            if clean_val in ('true', 'yes', 'on', '1'):
                config[key] = True
            elif clean_val in ('false', 'no', 'off', '0'):
                config[key] = False
    
    # Ensure video extension always starts with a leading dot
    if 'video_extension' in config and isinstance(config['video_extension'], str) and not config['video_extension'].startswith('.'):
        config['video_extension'] = '.' + config['video_extension']
    
    # Ensure GCP bucket ends with a "/" to be treated as a prefix
    if config.get('gcp_upload', False) and 'gcp_bucket_path' in config:
        config['gcp_bucket_path'] = config['gcp_bucket_path'].rstrip('/') + '/'

def load_config(config_path: str = 'configurations.yml'):
    """
    Loads and verifies the YAML configuration file.
    
    Arguments
    ---------
    config_path (str): The file path to the YAML configuration file. Defaults
            to 'configurations.yml'.
    Returns
    -------
    dict
    """
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    clean_and_validate_config(config=config)
    return config

def get_gpu_type():
    """
    Queries nvidia-smi to determine if the installed GPU is a 'Professional' 
    model with unlimited/high session limits.

    Returns
    -------
    str, 'PRO' for professional grade mode, 'CONSUMER' for consumer grade, or
    'UNKNOWN' if unknown or no GPU is found
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, check=True
        )
        gpu_name = result.stdout.lower()
        
        pro_identifiers = ['rtx 6000', 'rtx 5000', 'quadro', 'tesla', 'a-series', 'ada generation']
        
        if any(ident in gpu_name for ident in pro_identifiers):
            return "PRO"
        return "CONSUMER"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "UNKNOWN"

def get_video_metadata(file_path: str, ffprobe_path: str):
    """Uses ffprobe to get internal metadata of a video file.
    
    Arguments
    ---------
    file_path (str): file path to the video from which to extract metadata
    ffprobe_path (str): file path to the `ffprobe` executable

    Returns
    -------
    list: [duration, fps, bit_rate, width, height]
    """
    cmd = [
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    duration = float(data['format']['duration'])
    bit_rate = int(data['format']['bit_rate'])
    
    width = 0
    height = 0
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            width = int(stream.get('width'))
            height = int(stream.get('height'))
            fps_str = stream.get('avg_frame_rate')
            if '/' in fps_str:
                num, den = map(int, fps_str.split('/'))
                fps = num / den
            else:
                fps = float(fps_str)
            break
    
    return duration, fps, bit_rate, width, height

def calculate_file_size(duration: float | int, bit_rate: float | int) -> int:
    """Calculates the file size in bytes based on duration and bit rate."""
    return int((duration * bit_rate) / 8)

def log_and_print(message: str, log_path: str, indent_spaces: int = 0):
    """Indents and writes a message to both console and log."""
    indent = " " * indent_spaces
    clean_msg = textwrap.indent(textwrap.dedent(message), indent)
    tqdm.write(clean_msg + "\n")
    with open(log_path, "a") as log:
        log.write(clean_msg)

def check_gcp_auth(bucket_path: str) -> bool:
    """Verifies Google Cloud storage access before starting.
    
    Automatically prompts for 'gcloud auth login' if authentication tokens 
    have expired or require refreshing.
    
    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket
    """
    gcloud_exec = shutil.which("gcloud")
    
    if not gcloud_exec:
        print("\n❌ ERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?")
        return False

    # Isolate the root bucket path (e.g., 'gs://bucket-name/') to separate auth from folder existence
    match = re.match(r'^(gs://[^/]+)', bucket_path)
    root_bucket = match.group(1) + '/' if match else bucket_path

    try:
        # Verify system authentication and base bucket accessibility
        root_result = subprocess.run([gcloud_exec, "storage", "ls", root_bucket], capture_output=True, text=True)
        
        # Check for authentication or token refresh failures
        if root_result.returncode != 0:
            stderr_text = (root_result.stderr or "").lower()
            auth_triggers = [
                "reauthentication failed", 
                "gcloud auth login", 
                "refreshing your current auth tokens",
                "invalid_grant",
                "unauthenticated",
                "401"
            ]
            
            if any(trigger in stderr_text for trigger in auth_triggers):
                print("\n==========================================================================")
                print("  ACTION REQUIRED: Google Cloud Re-authentication Needed")
                print("==========================================================================")
                print("[!] GCP credentials expired or require re-authentication.")
                print("[->] Launching interactive 'gcloud auth login'...\n")
                
                # Run gcloud auth login interactively (allows browser/console prompt)
                auth_result = subprocess.run([gcloud_exec, "auth", "login"])
                
                if auth_result.returncode == 0:
                    print("\n[+] Re-authentication successful! Retrying cloud storage connection...\n")
                    root_result = subprocess.run([gcloud_exec, "storage", "ls", root_bucket], capture_output=True, text=True)
                else:
                    print("\n❌ ERROR: Google Cloud authentication failed or was canceled.")
                    return False
        
        if root_result.returncode != 0:
            print("\n[!] GCP AUTHENTICATION ERROR: Failed to connect to the cloud storage system.")
            print("Please verify your gcloud authentication credentials, login status, or bucket permissions.")
            print(f"Details: {root_result.stderr.strip()}")
            return False
            
        # If credentials are valid, verify if the explicit subfolder prefix exists
        path_result = subprocess.run([gcloud_exec, "storage", "ls", bucket_path], capture_output=True, text=True)
        
        if path_result.returncode != 0:
            print("\n[!] GCP CONFIGURATION ERROR: Target folder path does not exist.")
            print(f"  -> Specified destination: {bucket_path}")
            print("\nTo prevent configuration typos from cluttering the cloud bucket layout,")
            print("this utility will not automatically initialize new directory prefixes.")
            print("Please double-check your spelling in the YAML config file, or manually create")
            print("the destination folder via the GCP Console or CLI before running this pipeline.")
            return False
            
        return True
        
    except Exception as e:
        print(f"\nERROR: An unexpected error occurred while checking GCP: {e}")
        return False

def get_ffmpeg_command(config: dict, tool: Literal["ffmpeg", "ffprobe"] = "ffmpeg"):
    """Finds ffmpeg or ffprobe."""
    extension = ".exe" if sys.platform.startswith("win") else ""
    executable_name = f"{tool}{extension}"

    local_path = os.path.join(os.getcwd(), "ffmpeg", "bin", executable_name)
    if os.path.exists(local_path):
        return local_path
        
    config_key = f"{tool}_path"
    config_val = config[config_key]
    if config_val and os.path.exists(config_val):
        return config_val
        
    return tool

def time_ceiling(time_str: str) -> str:
    """Round time stamp up to the nearest 30 seconds."""
    time_split, _, frame = time_str.rpartition(':')
    new_time = pd.to_datetime(time_split, format="%H:%M:%S")
    if int(frame) > 0:
        new_time += pd.Timedelta(seconds=1)
    new_time = new_time.ceil('30s')
    return new_time.strftime("%H:%M:%S") + ":00"

def timestamp_to_seconds(timestamp_str: str, fps: float | int) -> float:
    """Converts HH:MM:SS:FF (frames) to total seconds (float)."""
    parts = timestamp_str.split(':')
    h, m, s, f = map(int, parts)
    return h * 3600 + m * 60 + s + (f / fps)

def seconds_to_timestamp(seconds: float | int, fps: float | int) -> str:
    """Converts seconds to HH:MM:SS:FF."""
    total_frames = int(seconds * fps + 1e-6)
    f = total_frames % round(fps)
    total_seconds = total_frames // round(fps)
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h:02}:{m:02}:{s:02}:{f:02}"

def get_gopro_sort_key(filename: str):
    """Parses GoPro filenames for correct sorting."""
    match = re.search(r'([A-Z]{2})(\d{2})(\d{4})', filename.upper())
    if match:
        _, chapter, rec_id = match.groups()
        return (int(rec_id), int(chapter))
    return (0, 0)

def init_worker():
    """Initializer for child pool workers to ignore SIGINT (Ctrl+C)."""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)

# =============================================================================
# WORKER TASK: PROCESS SINGLE DEPLOYMENT
# =============================================================================

def process_single_deployment(row: dict, config: dict, ffmpeg_exe: str, ffprobe_exe: str, process: bool, remote_inventory: set = None) -> dict:
    """Standalone task for processing one deployment."""
    if config['diagnostic_mode']:
        iter_start = time.perf_counter()
    folder_id = str(row[config['col_folder_name']]).strip()
    start_time_ceil = str(row['start_time_ceil']).strip()

    log_payload = ""

    folder_path = os.path.join(config['input_directory'], folder_id)
    if not os.path.exists(folder_path):
        log_payload += f"SKIP: Folder {folder_path} not found.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Video folder path not found", "log_payload": log_payload}
        
    output_path = os.path.join(
        config['output_directory'], f"{folder_id}{config['video_extension']}"
        )

    already_uploaded = False
    if config['gcp_upload'] and config['delete_local_after_upload']:
        remote_filename = f"{folder_id}{config['video_extension']}"
        if remote_inventory is not None and remote_filename in remote_inventory:
            already_uploaded = True

    if not config['reprocess'] and (os.path.exists(output_path) or already_uploaded):
        log_payload += f"Deployment {folder_id} already exists locally or on GCP bucket directory. Skipping.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Output video already exists", "log_payload": log_payload}

    if not os.path.exists(folder_path):
        log_payload += f"SKIP: Folder {folder_id} not found.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Folder path not found", "log_payload": log_payload}

    video_files = [f for f in os.listdir(folder_path) 
                    if f.upper().endswith(config['video_extension'].upper())]
    if not video_files:
        log_payload += f"SKIP: No videos in {folder_id}.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "No matched video extension files inside directory", "log_payload": log_payload}
    video_files.sort(key=get_gopro_sort_key)

    first_file_path = os.path.join(folder_path, video_files[0])
    
    try:
        _, source_fps, _, _, _ = get_video_metadata(file_path=first_file_path, ffprobe_path=ffprobe_exe)
    except Exception as e:
        log_payload += f"ERROR: Primary metadata extraction failed on first file {first_file_path} in folder {folder_id}. Details: {str(e)}\n"
        return {
            "status": "ERROR",
            "folder_id": folder_id,
            "error_type": "Metadata Corruption (FFprobe Failure)",
            "error_msg": f"Failed to parse initial file structure for '{os.path.basename(first_file_path)}'. Underlying crash: {type(e).__name__}: {str(e)}",
            "log_payload": log_payload
        }

    start_time_fps = float(config['start_time_fps'])
    raw_target = str(config['output_fps']).lower()
    output_fps = source_fps if raw_target == 'auto' else float(raw_target)

    time_scaling = start_time_fps / source_fps
    
    pd_start_seconds = timestamp_to_seconds(timestamp_str=start_time_ceil, fps=start_time_fps)
    pd_start_seconds += (int(config['time_buffer_minutes']) * 60)
    pd_duration_seconds = int(config['video_duration_minutes']) * 60

    nudge = 0.2 / start_time_fps
    padding = 0.1
    start_seconds = (pd_start_seconds - nudge) * time_scaling
    video_duration_sec = (pd_duration_seconds * time_scaling)
    end_seconds = start_seconds + video_duration_sec + nudge + padding

    if config['diagnostic_mode']:
        tqdm.write(f"  > Probing metadata for {len(video_files)} video chapters in {folder_id}...")
    file_data = []
    for f in video_files:
        full_p = os.path.join(folder_path, f)
        
        try:
            dur, source_fps, br, w, h = get_video_metadata(file_path=full_p, ffprobe_path=ffprobe_exe)
        except Exception as e:
            log_payload += f"ERROR: Metadata extraction failed on file {full_p}. Details: {str(e)}\n"
            return {
                "status": "ERROR",
                "folder_id": folder_id,
                "error_type": "Metadata Corruption (FFprobe Failure)",
                "error_msg": f"Failed to parse intermediate chapter file components for '{f}'. Structural data is likely missing or corrupt. Underlying crash: {type(e).__name__}: {str(e)}",
                "log_payload": log_payload
            }

        file_data.append({
            'path': full_p,
            'duration': dur,
            'fps': source_fps,
            'bit_rate': br,
            'width': w,
            'height': h
        })

    if config['diagnostic_mode']:
        tqdm.write("  > Determining needed files and trim points...")
    cumulative_time = 0
    needed_files = []
    for data in file_data:
        file_start = cumulative_time
        file_end = cumulative_time + data['duration']
        
        if file_end > start_seconds and file_start < end_seconds:
            rel_start = max(0, start_seconds - file_start)
            rel_end = min(data['duration'], end_seconds - file_start)

            bpp = data['bit_rate'] / (data['width'] * data['height'] * data['fps'])
            
            needed_files.append({
                'path': data['path'], 
                'ss': rel_start, 
                't': rel_end - rel_start,
                'size': calculate_file_size(rel_end - rel_start, data['bit_rate']),
                'bpp': bpp,
                'width': data['width'],
                'height': data['height'],
                'fps': data['fps'],
                'bit_rate': data['bit_rate']
            })
        cumulative_time = file_end

    if config['skip_partial_videos']:
        total_clipped_seconds = sum(f_info['t'] for f_info in needed_files)
        expected_seconds = pd_duration_seconds * time_scaling
        
        if total_clipped_seconds < (expected_seconds - 2.0):
            log_payload += (
                f"SKIP: {folder_id} - Insufficient footage. Found only "
                f"{total_clipped_seconds / 60:.2f} mins out of required {config['video_duration_minutes']} mins.\n"
            )
            return {
                "status": "SKIP", 
                "folder_id": folder_id, 
                "reason": "Insufficient video footage", 
                "log_payload": log_payload
            }

    if not needed_files:
        log_payload += f"SKIP: {folder_id} - No footage found for the requested time window.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "No overlapping footage found within clipping window", "log_payload": log_payload}

    cumulative_size = 0
    cumulative_bpp = 0
    input_args = []
    filter_complex_parts = []
    filter_inputs = ""

    for i, f_info in enumerate(needed_files):
        cumulative_size += f_info['size']
        cumulative_bpp += f_info['bpp']
        input_args.extend(["-i", f_info['path']])

        v_label = f"[v{i}]"
        filter_complex_parts.append(
            f"[{i}:v]trim=start={f_info['ss']}:duration={f_info['t']},"
            f"setpts=PTS-STARTPTS{v_label}"
        )
        
        if config['include_audio']:
            a_label = f"[a{i}]"
            filter_complex_parts.append(
                f"[{i}:a]atrim=start={f_info['ss']}:duration={f_info['t']},"
                f"asetpts=PTS-STARTPTS{a_label}"
            )
            filter_inputs += f"{v_label}{a_label}"
        else:
            filter_inputs += v_label

    target_bitrate = f"{int(cumulative_size * 8 / (pd_duration_seconds * time_scaling))}"
    is_auto_mode = str(config['quality_crf']).lower() == 'auto'
    if is_auto_mode and config['diagnostic_mode']:
        tqdm.write(f"  > Targeting bitrate {int(target_bitrate)/1_000_000:.2f} Mbps to match source density.")

    fps_logic = f"fps=fps={output_fps}:round=near"
    if raw_target != 'auto':
        fps_logic += f",setpts=N/({output_fps}*TB)"

    if config['include_audio']:
        concat_part = f"{filter_inputs}concat=n={len(needed_files)}:v=1:a=1[v_stitched][outa]"
        filter_complex_parts.append(concat_part)
        filter_complex_parts.append(f"[v_stitched]{fps_logic}[outv]")
        audio_args = ["-map", "[outa]"]
    else:
        concat_part = f"{filter_inputs}concat=n={len(needed_files)}:v=1,{fps_logic}[outv]"
        filter_complex_parts.append(concat_part)
        audio_args = ["-an"]
        
    maparg = "[outv]"

    filter_str = "; ".join(filter_complex_parts)

    cumulative_output_frames = 0
    target_total_frames = int(pd_duration_seconds * start_time_fps)
    table_lines = []
    table_lines.append(f"\n{'='*80}")
    table_lines.append(f"{'QC SEAM INSPECTION TABLE - Folder: ' + folder_id + ' (' + str(config['video_duration_minutes']) + ' min)':^80}")
    table_lines.append(f"{'='*80}")
    table_lines.append(f"{'NEW VIDEO TIME':<18} | {'ACTION':<17} | {'SOURCE FILE':<17} | {'SOURCE TIMESTAMP'}")
    table_lines.append(f"{'-'*19}|{'-'*19}|{'-'*19}|{'-'*20}")

    for i, segment in enumerate(needed_files):
        start_ts = seconds_to_timestamp(cumulative_output_frames / start_time_fps, start_time_fps)
        report_ss = segment['ss'] + (nudge * time_scaling if i == 0 else 0)
        source_start = seconds_to_timestamp(report_ss / time_scaling, start_time_fps)
        table_lines.append(f"{start_ts:<18} | START SEGMENT     | {os.path.basename(segment['path']):<17} | {source_start}")
        
        actual_t = segment['t'] - (nudge * time_scaling if i == 0 else 0)
        segment_frames = round(actual_t * segment['fps'])
        
        if (cumulative_output_frames + segment_frames) > target_total_frames:
            segment_frames = target_total_frames - cumulative_output_frames
        
        last_frame_idx = cumulative_output_frames + segment_frames - 1
        end_ts = seconds_to_timestamp(last_frame_idx / start_time_fps, start_time_fps)
        
        last_frame_rel = (segment_frames - 1) / segment['fps']
        source_end = seconds_to_timestamp((report_ss + last_frame_rel) / time_scaling, start_time_fps)
        
        if i < len(needed_files) - 1:
            table_lines.append(f"{end_ts:<18} | LAST FRAME        | {os.path.basename(segment['path']):<17} | {source_end}")
            table_lines.append(f"{' '*18} |      -- SEAM --   | {' '*17} |")
        else:
            final_end_ts = seconds_to_timestamp(target_total_frames / start_time_fps, start_time_fps)
            table_lines.append(f"{final_end_ts:<18} | VIDEO END         | {os.path.basename(segment['path']):<17} | {source_end}")
        
        cumulative_output_frames += segment_frames

    table_lines.append(f"{'-'*80}")
    table_lines.append(f"TOTAL OUTPUT FRAMES: {cumulative_output_frames} / {target_total_frames}")
    table_lines.append(f"{'='*80}\n")
    full_table_str = "\n".join(table_lines)

    _, _, worker_free = shutil.disk_usage(config['output_directory'])
    if (worker_free // (2**30)) < config['min_gb_required'] and process:
        log_payload += f"SKIP: {folder_id} - Disk space critical ({worker_free // (2**30)}GB left).\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Disk space safety constraints tripped", "log_payload": log_payload}

    if config['use_gpu']:
        encoder_args = ["-c:v", "h264_nvenc", "-rc", "vbr"]
        if is_auto_mode:
            encoder_args += ["-b:v", target_bitrate, "-maxrate", "100M", "-bufsize", "100M"]
        else:
            encoder_args += ["-b:v", "0", "-cq", str(config['quality_crf'])]
        encoder_args += ["-preset", "p7"]
    else:
        encoder_args = ["-c:v", "libx264"]
        if is_auto_mode:
            encoder_args += ["-b:v", target_bitrate]
        else:
            encoder_args += ["-crf", str(config['quality_crf'])]
        encoder_args += ["-preset", "medium"]
    
    final_metadata_t = pd_duration_seconds if raw_target != 'auto' else (pd_duration_seconds * time_scaling)

    cmd = [
        ffmpeg_exe, "-y"
    ] + input_args + [
        "-filter_complex", filter_str,
        "-map", maparg
    ] + audio_args + encoder_args + [
        "-r", str(output_fps),
        "-fps_mode", "cfr",
        "-video_track_timescale", "30000",
        "-t", str(final_metadata_t),
        output_path
    ]

    result = MockResult()
    if process:
        max_attempts = int(config['max_retries']) + 1
        timeout_val = int(config['timeout_minutes']) * 60 if config['timeout_minutes'] else None
        
        for attempt in range(1, max_attempts + 1):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_val)
                break 
            except subprocess.TimeoutExpired:
                log_payload += f"TIMEOUT NOTICE: Deployment {folder_id} timed out on attempt {attempt}/{max_attempts}.\n"
                if attempt == max_attempts:
                    return {
                        "status": "ERROR", 
                        "folder_id": folder_id, 
                        "error_type": "FFmpeg Timeout", 
                        "error_msg": f"Execution halted after timing out consistently across {max_attempts} distinct attempts.",
                        "log_payload": log_payload
                    }
                time.sleep(5) 
                continue
                
        if result.returncode != 0:
            log_payload += f"ERROR in {folder_id}: {result.stderr}\n"
            return {
                "status": "ERROR", 
                "folder_id": folder_id, 
                "error_type": "FFmpeg Fatal Exit Code", 
                "error_msg": result.stderr.strip().split('\n')[-1],
                "log_payload": log_payload
            }
    
    if os.path.exists(output_path):
        actual_size = os.path.getsize(output_path)
        actual_bitrate = (actual_size * 8) / final_metadata_t
        actual_bpp = actual_bitrate / (needed_files[0]['width'] * needed_files[0]['height'] * output_fps)
    else:
        actual_size = 0
        actual_bpp = 0

    avg_bpp_src = cumulative_bpp / len(needed_files)
    expectations_table_lines = []
    expectations_table_lines.append(f"{' '*23} | EXPECTED {' '*7} ACTUAL")
    expectations_table_lines.append(f"    {'-'*20}|{'-'*30}")
    expectations_table_lines.append(f"    OUTPUT FILE SIZE    | {cumulative_size / (2**30):.2f} GB {' ':<4} --> {actual_size / (2**30):.2f} GB")
    expectations_table_lines.append(f"    BITRATE             | {int(target_bitrate)/1_000_000:.2f} Mbps {' ':<1} --> {actual_bitrate/1_000_000:.2f} Mbps")
    expectations_table_lines.append(f"    INFORMATION DENSITY | {avg_bpp_src:.4f} BPP {' ':<1} --> {actual_bpp:.4f} BPP")
    expectations_table_str = "\n".join(expectations_table_lines) +"\n"
    
    if config['diagnostic_mode']:
        iter_duration = time.perf_counter() - iter_start
        if iter_duration > 60:
            tqdm.write(f"  > Created {folder_id}{config['video_extension']} in {iter_duration/60:.2f} minutes.\n")
        else:
            tqdm.write(f"  > Created {folder_id}{config['video_extension']} in {iter_duration:.2f} seconds.\n")
        tqdm.write("    Output file metrics versus expectations:\n")
        tqdm.write(expectations_table_str)

    is_bpp_ideal_80 = avg_bpp_src * 0.80 <= actual_bpp <= avg_bpp_src * 1.20
    is_size_ideal_80 = cumulative_size * 0.80 <= actual_size <= cumulative_size * 1.20
    is_bpp_ideal_90 = avg_bpp_src * 0.90 <= actual_bpp <= avg_bpp_src * 1.10
    is_size_ideal_90 = cumulative_size * 0.90 <= actual_size <= cumulative_size * 1.10

    log_payload += f"\nSUMMARY OF FOLDER {folder_id}:\n"
    log_payload += f"    Estimated output video size without visual quality loss: {cumulative_size / (2**30):.2f} GB\n"
    log_payload += f"    Estimated target bitrate to maintain visual fidelity: {int(target_bitrate)/1_000_000:.2f} Mbps\n"
    log_payload += f"    Average information density of original videos: {avg_bpp_src:.4f} bits per pixel (BPP)\n"
    log_payload += ' '*30 + '* '*10 + '\n'
    log_payload += f"    Output video file size: {actual_size / (2**30):.2f} GB\n"
    log_payload += f"    Output video bitrate:   {actual_bitrate/1_000_000:.2f} Mbps\n"
    log_payload += f"    Information density:    {actual_bpp:.4f} BPP\n\n"
    log_payload += f"    -> Within 80% of original average BPP:  {'YES' if is_bpp_ideal_80 else 'NO  X'}\n"
    log_payload += f"    -> Within 80% of estimated file size:   {'YES' if is_size_ideal_80 else 'NO  X'}\n"
    log_payload += f"    -> Within 90% of original average BPP:  {'YES' if is_bpp_ideal_90 else 'NO  X'}\n"
    log_payload += f"    -> Within 90% of estimated file size:   {'YES' if is_size_ideal_90 else 'NO  X'}\n"

    if result.returncode == 0:
        log_payload += full_table_str
        if config['diagnostic_mode']:
            tqdm.write(full_table_str)

    return {
        "status": "SUCCESS", 
        "folder_id": folder_id, 
        "output_path": output_path,
        "targets": {
            "bpp_80": is_bpp_ideal_80, "size_80": is_size_ideal_80,
            "bpp_90": is_bpp_ideal_90, "size_90": is_size_ideal_90
        },
        "log_payload": log_payload
    }

# =============================================================================
# MAIN ROUTINE
# =============================================================================

def process_deployments(config_path: str = 'configurations.yml', process=True):
    """Orchestrates parallel processing and returns True if no critical errors occurred."""
    process_start = time.perf_counter()
    
    config = load_config(config_path.strip('"'))
    CONFIG_DEFAULTS = {
        'clear_log': False,
        'delete_local_after_upload': False,
        'diagnostic_mode': False,
        'gcp_upload': False,
        'include_audio': False,
        'log_file': 'processing_log.txt',
        'max_retries': 2,
        'min_gb_required': 10,
        'num_workers': 1,
        'output_fps': 'auto',
        'quality_crf': 'auto',
        'reprocess': False,
        'skip_partial_videos': True,
        'start_time_fps': 30,
        'time_buffer_minutes': -2,
        'timeout_minutes': 60,
        'use_gpu': False,
        'video_duration_minutes': 24,
        'video_extension': '.MP4'
    }
    config = CONFIG_DEFAULTS | config
    
    ffmpeg_exe = get_ffmpeg_command(config=config, tool="ffmpeg")
    ffprobe_exe = get_ffmpeg_command(config=config, tool="ffprobe")

    max_cpu = os.cpu_count() or 1
    gpu_status = get_gpu_type() if config['use_gpu'] else "N/A"

    if config['use_gpu']:
        if gpu_status == "PRO":
            max_allowed = max_cpu 
        else:
            max_allowed = 12
        if config['num_workers'] > max_allowed:
            print(f"NOTICE: num_workers ({config['num_workers']}) exceeds hardware safety limit. Capping at {max_allowed}.", flush=True)
            config['num_workers'] = max_allowed
    else:
        max_allowed = max(1, max_cpu - 1)
        if config['num_workers'] > max_allowed:
            print(f"  > NOTICE: num_workers ({config['num_workers']}) exceeds hardware limit ({max_cpu}). Capping at {max_allowed}.", flush=True)

    os.makedirs(config['output_directory'], exist_ok=True)
    _, _, free = shutil.disk_usage(config['output_directory'])
    if free // (2**30) < config['min_gb_required']:
        print(f"\n[!] FATAL ERROR: Insufficient disk space ({free // (2**30)}GB remaining). Stopping.")
        return False
    
    # Confirm GCP bucket authentication (ONLY if gcp_upload is True)
    if config['gcp_upload']:
        gcloud_exec = shutil.which("gcloud")
        if 'gcp_bucket_path' not in config:
            print("\nERROR: `gcp_upload` set to True but no GCP bucket was defined.")
            return False
        if not check_gcp_auth(bucket_path=config['gcp_bucket_path']):
            return False

    mode = "w" if config['clear_log'] else "a"
    with open(config['log_file'], mode) as log:
        log.write(f"{'#'*80}\nSESSION START: {datetime.now()}\n")
        log.write(f"CONFIGURATION: {config_path}\n\n")
        for key, value in config.items():
            log.write(f"  -> {key}: {value}\n")
        log.write("\n")
        log.write(f"{'#'*80}\n\n")

    remote_inventory = None

    if config['gcp_upload'] and config['delete_local_after_upload']:
        print("  > Mapping remote bucket directory to cache existing deployments...", flush=True)
        ls_remote = subprocess.run(
            [gcloud_exec, "storage", "ls", config['gcp_bucket_path']],
            capture_output=True, text=True
        )
        remote_inventory_set = set()
        if ls_remote.returncode == 0:
            for line in ls_remote.stdout.splitlines():
                filename = line.strip().split('/')[-1]
                if filename:
                    remote_inventory_set.add(filename)
        remote_inventory = remote_inventory_set

    try:
        df = pd.read_csv(config['csv_path'], encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(config['csv_path'], encoding='ISO-8859-1')
    df.columns = df.columns.str.strip()
    df[config['col_folder_name']] = df[config['col_folder_name']].str.strip()
    df['start_time_ceil'] = df[config['col_start_time']].apply(time_ceiling)

    print(f"  > Processing {int(df.shape[0])} deployments. Monitoring progress...\n", flush=True)
    tasks = df.to_dict('records')
    failed_uploads, overall_success = [], True

    errors_by_type = defaultdict(list)
    missed_metrics = defaultdict(list)
    skipped_deployments = defaultdict(list)
    success_count = 0

    bar_format = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}"
    pbar = tqdm(total=len(tasks), position=0, desc="Encoding Video", bar_format=bar_format)

    from concurrent.futures import wait, FIRST_COMPLETED

    upload_queue = []
    with ProcessPoolExecutor(max_workers=config['num_workers'], initializer=init_worker) as executor:
        futures_map = {}
        for row in tasks:
            future = executor.submit(
                process_single_deployment, 
                row, config, ffmpeg_exe, ffprobe_exe, 
                process, remote_inventory
            )
            futures_map[future] = row

        futures_list = list(futures_map.keys())

        while futures_list:
            done, _ = wait(futures_list, timeout=0.5, return_when=FIRST_COMPLETED)
            
            for future in done:
                futures_list.remove(future)
                result = future.result()
                folder_id = result['folder_id']
                
                pbar.update(1)
                
                if result.get('log_payload'):
                    with open(config['log_file'], "a") as log:
                        log.write(result['log_payload'])
                
                if result['status'] == "ERROR":
                    err_type = result.get('error_type', 'Unclassified Functional Error')
                    err_msg = result.get('error_msg', 'No trace log strings provided.')
                    errors_by_type[err_type].append(f"{folder_id} -> {err_msg}")
                    overall_success = False
                    
                elif result['status'] == "SKIP":
                    reason = result.get('reason', 'Skipped')
                    skipped_deployments[reason].append(folder_id)
                    
                elif result['status'] == "SUCCESS":
                    success_count += 1
                    upload_queue.append((folder_id, result['output_path']))
                    
                    t_flags = result.get('targets', {})
                    missed_list = []
                    if not t_flags.get('bpp_80'):
                        missed_list.append("Missed 80% BPP target")
                    elif not t_flags.get('bpp_90'):
                        missed_list.append("Missed 90% BPP target")
                    if not t_flags.get('size_80'):
                        missed_list.append("Missed 80% file size target")
                    elif not t_flags.get('size_90'):
                        missed_list.append("Missed 90% file size target")
                    
                    if missed_list:
                        missed_metrics[folder_id] = missed_list

    pbar.close()

    summary_lines = []
    summary_lines.append(f"\n{'='*80}\n{'FINAL BATCH PROCESSING EXECUTION SUMMARY REPORT':^80}\n{'='*80}")
    summary_lines.append(f"Successfully processed deployments:  {success_count} / {len(tasks)}")
    summary_lines.append(f"Number of skipped deployments:       {sum(len(v) for v in skipped_deployments.values())}")
    summary_lines.append(f"Number of hard failures encountered: {sum(len(v) for v in errors_by_type.values())}")
    summary_lines.append(f"Number of quality target misses:     {len(missed_metrics)}")

    if skipped_deployments:
        summary_lines.append(f"\n{'-'*18} SKIPPED FILES {'-'*18}")
        for skip_reason, list_folders in skipped_deployments.items():
            summary_lines.append(f"  -> {skip_reason}: {len(list_folders)} deployments affected.")

    if errors_by_type:
        summary_lines.append(f"\n{'!'*15} ERRORS ENCOUNTERED {'!'*15}")
        for err_title, deployments in errors_by_type.items():
            summary_lines.append(f"\n * Error Category: {err_title} ({len(deployments)} deployments affected):")
            for d in deployments:
                summary_lines.append(f"   -> {d}")

    if missed_metrics:
        summary_lines.append(f"\n{'-'*10} QUALITY THRESHOLD (80% / 90%) MISSES {'-'*10}")
        for idx, (f_id, faults) in enumerate(missed_metrics.items(), start=1):
            summary_lines.append(f"  {idx}. Deployment {f_id}: {', '.join(str(x) for x in faults)}")

    master_summary_str = "\n".join(summary_lines)
    master_summary_str += "\n\n"
    with open(config['log_file'], "a") as log:
        log.write(master_summary_str)
    print(master_summary_str)

    process_duration = time.perf_counter() - process_start
    if process_duration > (60 * 60):
        process_msg = f"  Processed {success_count} deployments in {process_duration/60/60:.2f} hours.\n"
    elif process_duration > 60:
        process_msg = f"  Processed {success_count} deployments in {process_duration/60:.2f} minutes.\n"
    else:
        process_msg = f"  Processed {success_count} deployments in {process_duration:.2f} seconds.\n"
    log_and_print(process_msg, config['log_file'])

    # GCP CLOUD UPLOAD
    if config['gcp_upload'] and upload_queue and process:
        print("\n  > All encodings complete. Initializing sequential high-speed cloud uploads...\n", flush=True)
        upload_timer = time.perf_counter()
        pbar_up = tqdm(total=len(upload_queue), position=0, desc="Cloud Uploading", bar_format=bar_format)
        
        for folder_id, output_path in upload_queue:
            try:
                cmd_up = [gcloud_exec, "storage", "cp"]
                if not config['reprocess']:
                    cmd_up.append("--no-clobber")
                cmd_up.extend([output_path, config['gcp_bucket_path']])
                
                upload_env = os.environ.copy()
                upload_env["CLOUDSDK_STORAGE_PROCESS_COUNT"] = "16"
                upload_env["CLOUDSDK_STORAGE_THREAD_COUNT"] = "8"
                upload_env["CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED"] = "True"
                
                result_up = subprocess.run(cmd_up, env=upload_env)
                
                if result_up.returncode == 0:
                    tqdm.write(f"  > {folder_id} uploaded to GCP successfully.")
                    if config['delete_local_after_upload'] and os.path.exists(output_path):
                        os.remove(output_path)
                else:
                    failed_uploads.append(output_path)
                    tqdm.write(f"  [!] UPLOAD RESOURCE ERROR for {folder_id}")
            except Exception as e:
                failed_uploads.append(output_path)
                tqdm.write(f"  [!] UPLOAD CRASH for {folder_id}: {str(e)}")
            
            pbar_up.update(1)
        
        if not failed_uploads:
            upload_duration = time.perf_counter() - upload_timer
            if upload_duration > (60 * 60):
                upload_msg = f"  > Uploaded {len(upload_queue)} videos in {upload_duration/60/60:.2f} hours.\n"
            elif upload_duration > 60:
                upload_msg = f"  > Uploaded {len(upload_queue)} videos in {upload_duration/60:.2f} minutes.\n"
            else:
                upload_msg = f"  > Uploaded {len(upload_queue)} videos in {upload_duration:.2f} seconds.\n"
            log_and_print(upload_msg, config['log_file'])
            pbar_up.close()
            
    if process and config['gcp_upload'] and failed_uploads:
        gcloud_exec = shutil.which("gcloud")
        for path in failed_uploads:
            cmd_retry = [gcloud_exec, "storage", "cp"]
            if not config['reprocess']:
                cmd_retry.append("--no-clobber")
            cmd_retry.extend([path, config['gcp_bucket_path']])
            
            upload_env = os.environ.copy()
            upload_env["CLOUDSDK_STORAGE_PROCESS_COUNT"] = "16"
            upload_env["CLOUDSDK_STORAGE_THREAD_COUNT"] = "8"
            upload_env["CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED"] = "True"
            
            retry = subprocess.run(cmd_retry, env=upload_env)
            if retry.returncode == 0:
                if config['delete_local_after_upload'] and os.path.exists(path):
                    os.remove(path)
            else:
                overall_success = False
        upload_duration = time.perf_counter() - upload_timer
        if upload_duration > (60 * 60):
            upload_msg = f"  Uploaded {len(upload_queue)} videos in {upload_duration/60/60:.2f} hours.\n"
        elif upload_duration > 60:
            upload_msg = f"  Uploaded {len(upload_queue)} videos in {upload_duration/60:.2f} minutes.\n"
        else:
            upload_msg = f"  Uploaded {len(upload_queue)} videos in {upload_duration:.2f} seconds.\n"
        log_and_print(upload_msg, config['log_file'])
        pbar_up.close()

    with open(config['log_file'], 'a') as log:
        log.write(f"\n{'='*80}\n")

    return (overall_success, os.path.basename(config['log_file']))

if __name__ == "__main__":
    args = parse_args()

    script_start = time.perf_counter()

    success, log = process_deployments(
        config_path=args.config_path, process=args.process
    )
    
    script_duration = time.perf_counter() - script_start
    if script_duration > (60 * 60):
        runtime = f"{script_duration/60/60:.2f} hours"
    elif script_duration > 60:
        runtime = f"{script_duration/60:.2f} minutes"
    else:
        runtime = f"{script_duration:.2f} seconds"

    status_msg = "SUCCESSFULLY COMPLETED" if success else "COMPLETED WITH FUNCTIONAL ERRORS"
    print(f"{status_msg}")
    print(f"Total overall runtime: {runtime}.")
    print(f"Review '{log}' for detailed metrics.", flush=True)