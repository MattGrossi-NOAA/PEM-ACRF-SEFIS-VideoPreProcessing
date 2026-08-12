"""
Cloud upload file check
-----------------------
A file inspection utility that compares file names and sizes between the input
(source) directory and the Google Cloud Project (GCP) storage bucket using the
same configuration YAML file as the original script.

Usage:
    python cloud-upload-check.py path/to/name-of-configuration-file.yml

Author:  matt.grossi at noaa.gov with creation and refactoring assistance from
         Google Gemini Coding Partner
Project: Southeast Fishery Independent Survey (SEFIS)
Version: 2026.3.0
Note:    Gemini Coding Partner was used to assist with developing this code.
         The code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

# =============================================================================
# PACKAGE DEPENDENCIES
# =============================================================================

from urllib.parse import urlparse
import subprocess
import yaml
import sys
import os
import csv
import shutil
import argparse
import posixpath

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
    return parser.parse_args()

def load_config(config_path: str = 'configurations.yml'):
    """Loads and verifies the YAML configuration file.
    
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

    # Validate keys
    REQUIRED_KEYS = {'output_directory', 'gcp_bucket_path'}
    missing = [f"  - '{k}'" for k in REQUIRED_KEYS if k not in config]
    if missing:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Missing required keys in YAML:\n" +
            "\n".join(missing) + "\n\n"
        )
        raise ValueError(error_msg)
    config['video_extension'] = config.get('video_extension', '.MP4')

    return config

def extract_gcp_prefix(bucket_path):
    """
    Safely extracts the folder prefix from a GCP bucket path, automatically
    stripping out wildcards, file names, or extensions.

    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket and folder

    Returns
    -------
    Returns the file prefix only
    """
    parsed_path = urlparse(bucket_path).path
    clean_path = parsed_path.lstrip('/')
    prefix = posixpath.dirname(clean_path)
    return f"{prefix.rstrip('/')}/" if prefix else ""

def find_gcloud_executable():
    """Locates the 'gcloud' executable via system PATH or standard install paths.
    
    Returns
    -------
    str or None: Absolute path to gcloud executable if found, otherwise None.
    """
    # 1. First check if gcloud is already available in the active session PATH
    gcloud_exec = shutil.which("gcloud")
    if gcloud_exec:
        return gcloud_exec

    # 2. Define standard fallback installation directories on Windows/Linux/macOS
    exec_name = "gcloud.cmd" if sys.platform.startswith("win") else "gcloud"
    possible_dirs = [
        os.path.join(os.getcwd(), "google-cloud-sdk", "bin"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin"),
        os.path.expandvars(r"%ProgramFiles%\Google\Cloud SDK\google-cloud-sdk\bin"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Cloud SDK\google-cloud-sdk\bin"),
    ]

    # 3. Search fallback directories
    for folder in possible_dirs:
        candidate = os.path.join(folder, exec_name)
        if os.path.exists(candidate):
            # Prepend directory to active session PATH for sub-processes
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            return candidate

    return None

def get_cloud_manifest(bucket_path, extension=None):
    """Queries GCP bucket and returns data in a dictionary.
    
    Automatically prompts for 'gcloud auth login' if authentication tokens 
    have expired or require refreshing.
    
    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket and folder to check
    extension (str): file extension to filter by (optional)

    Returns
    -------
    dict of file names and sizes 
    """
    bucket_path = f"{bucket_path.rstrip('/*')}/*"
    print(f"Fetching cloud bucket inventory from {bucket_path}...")
    
    gcloud_exec = find_gcloud_executable()
    if not gcloud_exec:
        print("\n❌ ERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?")
        sys.exit(1)

    cmd = [
        gcloud_exec, "storage", "objects", "list", 
        bucket_path, 
        '--format=csv[no-heading](name, size)'
    ]
    
    # Execute gcloud command
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Check for authentication or token refresh failures
    if result.returncode != 0:
        stderr_text = result.stderr or ""
        auth_triggers = [
            "reauthentication failed", 
            "gcloud auth login", 
            "refreshing your current auth tokens",
            "invalid_grant"
        ]
        
        if any(trigger in stderr_text.lower() for trigger in auth_triggers):
            print("\n==========================================================================")
            print("  ACTION REQUIRED: Google Cloud Re-authentication Needed")
            print("==========================================================================")
            print("[!] GCP credentials expired or require re-authentication.")
            print("[->] Launching interactive 'gcloud auth login'...\n")
            
            # Run gcloud auth login interactively (allows browser prompt)
            auth_cmd = [gcloud_exec, "auth", "login"]
            auth_result = subprocess.run(auth_cmd)
            
            if auth_result.returncode == 0:
                print("\n[+] Re-authentication successful! Retrying cloud bucket inventory check...\n")
                result = subprocess.run(cmd, capture_output=True, text=True)
            else:
                print("\n❌ ERROR: Google Cloud authentication failed or was canceled.")
                sys.exit(1)

    # If execution failed after re-auth attempt or failed for a non-auth reason
    if result.returncode != 0:
        print(f"\n❌ ERROR running gcloud command: {result.stderr}")
        sys.exit(1)

    # Parse output string into dictionary
    cloud_data = {}
    ext_lower = f".{extension.lstrip('.').lower()}" if extension else None
    reader = csv.reader(result.stdout.strip().splitlines())
    for row in reader:
        if row:
            path, size = row[0].strip(), row[1].strip()
            if not ext_lower or path.lower().endswith(ext_lower):
                cloud_data[path] = int(size)
            
    return cloud_data

def get_local_manifest(local_path, prefix, extension=None):
    """Scans the local top-level directory and returns data in a dictionary.
    
    Arguments
    ---------
    local_path (str): local directory to compare to cloud
    prefix (str): prefix appended to file name in the storage bucket
    extension (str): file extension to filter by (optional)
    """
    print(f"Scanning local files in {local_path}...")
    
    if not os.path.exists(local_path):
        print(f"\n❌ ERROR: Cannot access local path: {local_path}")
        sys.exit(1)

    ext_lower = extension.lower() if extension else None    
    local_data = {}
    for file in os.listdir(local_path):
        full_path = os.path.join(local_path, file)
        
        if os.path.isfile(full_path) and (not ext_lower or file.lower().endswith(ext_lower)):
            try:
                size = os.path.getsize(full_path)
                gcp_style_path = f"{prefix}{file}".replace("//", "/")
                local_data[gcp_style_path] = size
            except Exception as e:
                print(f"  ⚠️ Error reading size for {file}: {e}")
                
    return local_data

def compare_inventories(local, cloud, extension):
    """Compares local file names and sizes with those in the cloud and prints
    results.
    
    Arguments
    ---------
    local (dict): dictionary containing `file_name: file_size` pairs for all
        local files
    cloud (dict): dictionary containing `file_name: file size` pairs for all
        cloud files
    extension (str): file extension
    """

    print("\nAnalyzing discrepancies...")
    
    missing_in_cloud = [p for p in local if p not in cloud]
    missing_in_local = [p for p in cloud if p not in local]

    size_mismatches = []
    for p, local_size in local.items():
        if p in cloud and cloud[p] != local_size:
            size_mismatches.append((p, local_size, cloud[p]))
    ext = extension.lstrip('.')

    print("\n=====================================")
    print("        COMPARISON RESULTS           ")
    print("=====================================")
    print(f"Local drive count: {len(local)} {ext} files")
    print(f"GCP Bucket count:  {len(cloud)} {ext} objects")
    print("-------------------------------------")

    if not missing_in_cloud and not missing_in_local and not size_mismatches:
        print("🎉 SUCCESS! Every file matches perfectly in name and size.")
    else:
        if missing_in_cloud:
            print(f"❌ Missing in Cloud ({len(missing_in_cloud)}):")
            for item in missing_in_cloud[:10]:
                print(f"  - {item}")
            if len(missing_in_cloud) > 10:
                print(f"  ... and {len(missing_in_cloud)-10} more.")
            
        if missing_in_local:
            print(f"⚠️ Extra in Cloud / Missing Locally ({len(missing_in_local)}):")
            for item in missing_in_local[:10]:
                print(f"  - {item}")
            if len(missing_in_local) > 10:
                print(f"  ... and {len(missing_in_local)-10} more.")
            
        if size_mismatches:
            print(f"❌ Byte Size Mismatches ({len(size_mismatches)}):")
            for item, l_sz, c_sz in size_mismatches[:10]:
                print(f"  - {item} (Local: {l_sz} bytes, Cloud: {c_sz} bytes)")
            if len(size_mismatches) > 10:
                print(f"  ... and {len(size_mismatches)-10} more.")

if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config_path)

    cloud_inventory = get_cloud_manifest(
        bucket_path=config['gcp_bucket_path'],
        extension=config['video_extension']
        )
    prefix = extract_gcp_prefix(config['gcp_bucket_path'])
    local_inventory = get_local_manifest(
        local_path=config['output_directory'],
        prefix=prefix,
        extension=config['video_extension']
        )
    
    compare_inventories(
        local=local_inventory,
        cloud=cloud_inventory,
        extension=config['video_extension']
        )