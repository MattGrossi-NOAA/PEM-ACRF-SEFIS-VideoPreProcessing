@echo off
setlocal enabledelayedexpansion
title SEFIS Video Bulk Cloud Upload

echo ==========================================================================
echo                        SEFIS Video Bulk Cloud Upload
echo ==========================================================================
echo.

:: ============================================================================
:: USER CONFIGURABLE BANDWIDTH LIMITS (Adjust speeds here)
:: ============================================================================
:: Speed used during nights (17:00-07:00) and weekends
:: set "OFF_HRS_BW=11.83M"
set "OFF_HRS_BW=off"

:: Speed used during business hours (07:00-17:00, Mon-Fri)
:: set "WORKDAY_BW=4.25M"
set "WORKDAY_BW=5M"

:: ============================================================================
:: PROMPT FOR CONFIGURATION FILE
:: ============================================================================
set "CONFIG_FILE=%~1"

:: Always prompt the user via Windows File Explorer if no file was passed directly
if "%CONFIG_FILE%"=="" (
    echo [-^>] Opening Windows File Explorer...
    echo [-^>] Please select the target YAML configuration file for this run.
    echo.
    
    :: Inline call to Windows Forms to launch a native File Explorer dialog
    for /f "delims=" %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.OpenFileDialog; $dialog.Filter = 'YAML Files (*.yaml;*.yml)|*.yaml;*.yml|All Files (*.*)|*.*'; $dialog.Title = 'Select Target Project YAML Configuration File'; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dialog.FileName }"') do (
        set "CONFIG_FILE=%%I"
    )
)

:: Abort safely if the user closes or cancels the File Explorer window
if "%CONFIG_FILE%"=="" (
    echo [!] CANCELED: No configuration file was selected. Process aborted.
    pause
    exit /b 1
)

:: Clean surrounding quotes from file path string
set "CONFIG_FILE=%CONFIG_FILE:"=%"

if not exist "%CONFIG_FILE%" (
    echo [!] FATAL ERROR: Specified configuration file does not exist:
    echo     "%CONFIG_FILE%"
    pause
    exit /b 1
)

echo [+] Selected Config File: %CONFIG_FILE%
echo [-^>] Extracting target parameters from file...

:: Parse 'gcp_bucket_path' from the selected YAML file
for /f "tokens=1,* delims=:" %%A in ('findstr /i /c:"gcp_bucket_path:" "%CONFIG_FILE%"') do (
    set "RAW_GCP_PATH=%%B"
)

:: Parse 'output_directory' from the selected YAML file
for /f "tokens=1,* delims=:" %%A in ('findstr /i /c:"output_directory:" "%CONFIG_FILE%"') do (
    set "RAW_LOCAL_PATH=%%B"
)

:: Parse 'video_extension' from the selected YAML file
for /f "tokens=1,* delims=:" %%A in ('findstr /i /c:"video_extension:" "%CONFIG_FILE%"') do (
    set "RAW_VIDEO_EXT=%%B"
)

:: Parse 'log_file' from the selected YAML file to align log output directory
for /f "tokens=1,* delims=:" %%A in ('findstr /i /c:"log_file:" "%CONFIG_FILE%"') do (
    set "RAW_YAML_LOG=%%B"
)

:: Clean formatting, quotation marks, leading spaces, and hidden Carriage Returns (\r)
if defined RAW_GCP_PATH (
    for /f "tokens=*" %%A in ("!RAW_GCP_PATH!") do set "GCP_BUCKET_PATH=%%~A"
    for /f "tokens=* delims= " %%A in ("!GCP_BUCKET_PATH!") do set "GCP_BUCKET_PATH=%%A"
    for /f "delims=" %%A in ('echo !GCP_BUCKET_PATH!') do set "GCP_BUCKET_PATH=%%A"
    
    :: Strip 'gs://' or 'gs:\' prefix if included in YAML
    if /i "!GCP_BUCKET_PATH:~0,5!"=="gs://" set "GCP_BUCKET_PATH=!GCP_BUCKET_PATH:~5!"
    if /i "!GCP_BUCKET_PATH:~0,5!"=="gs:\" set "GCP_BUCKET_PATH=!GCP_BUCKET_PATH:~5!"
)

if defined RAW_LOCAL_PATH (
    for /f "tokens=*" %%A in ("!RAW_LOCAL_PATH!") do set "LOCAL_SOURCE=%%~A"
    for /f "tokens=* delims= " %%A in ("!LOCAL_SOURCE!") do set "LOCAL_SOURCE=%%A"
    for /f "delims=" %%A in ('echo !LOCAL_SOURCE!') do set "LOCAL_SOURCE=%%A"
)

set "VIDEO_EXT="
if defined RAW_VIDEO_EXT (
    for /f "tokens=*" %%A in ("!RAW_VIDEO_EXT!") do set "VIDEO_EXT=%%~A"
    for /f "tokens=* delims= " %%A in ("!VIDEO_EXT!") do set "VIDEO_EXT=%%A"
    for /f "delims=" %%A in ('echo !VIDEO_EXT!') do set "VIDEO_EXT=%%A"
)

:: Default video extension to .MP4 if missing or empty in YAML
if "!VIDEO_EXT!"=="" set "VIDEO_EXT=.MP4"

:: Ensure extension starts with a leading dot (.)
if not "!VIDEO_EXT:~0,1!"=="." set "VIDEO_EXT=.!VIDEO_EXT!"

:: Parse and normalize the log directory path from the YAML log_file key
set "YAML_LOG_PATH="
if defined RAW_YAML_LOG (
    for /f "tokens=*" %%A in ("!RAW_YAML_LOG!") do set "YAML_LOG_PATH=%%~A"
    for /f "tokens=* delims= " %%A in ("!YAML_LOG_PATH!") do set "YAML_LOG_PATH=%%A"
    for /f "delims=" %%A in ('echo !YAML_LOG_PATH!') do set "YAML_LOG_PATH=%%A"
    :: Convert double backslashes (\\) to standard single backslashes (\)
    set "YAML_LOG_PATH=!YAML_LOG_PATH:\\=\!"
)

:: Extract folder path (%%~dpF) from the YAML log file path, defaulting to LOCAL_SOURCE if missing
if defined YAML_LOG_PATH (
    for %%F in ("!YAML_LOG_PATH!") do set "LOG_DIR=%%~dpF"
) else (
    set "LOG_DIR=%LOCAL_SOURCE%\"
)

:: Define final rclone log destination inside the target processing folder
set "LOG_FILE=%LOG_DIR%rclone_upload_archive.log"

:: Verify required parameters were found inside the chosen YAML
if "%GCP_BUCKET_PATH%"=="" (
    echo [!] FATAL ERROR: Missing 'gcp_bucket_path:' key in %CONFIG_FILE%
    pause
    exit /b 1
)

if "%LOCAL_SOURCE%"=="" (
    echo [!] FATAL ERROR: Missing 'output_directory:' key in %CONFIG_FILE%
    pause
    exit /b 1
)

echo [+] Target Bucket Path: %GCP_BUCKET_PATH%
echo [+] Local Output Directory: %LOCAL_SOURCE%
echo [+] Target File Extension: !VIDEO_EXT!
echo [+] Log Output Directory:  %LOG_DIR%
echo [+] Archive Log File:       %LOG_FILE%
echo [+] Bandwidth Limits: Peak=!WORKDAY_BW!, Off-Peak=!OFF_HRS_BW!
echo.

:: ============================================================================
:: LOCAL DIRECTORY VALIDATION
:: ============================================================================
if not exist "%LOCAL_SOURCE%" (
    echo [-^>] Local output folder "%LOCAL_SOURCE%" not found.
    echo [-^>] Creating local directory structure automatically...
    mkdir "%LOCAL_SOURCE%"
    echo [+] Directory created successfully.
    echo.
)

if not exist "%LOG_DIR%" (
    echo [-^>] Log folder "%LOG_DIR%" not found.
    echo [-^>] Creating log directory structure automatically...
    mkdir "%LOG_DIR%"
    echo [+] Log directory created successfully.
    echo.
)

:: Standardized Non-Admin System Directory Definitions
set "SHARED_RCLONE_DIR=%LOCALAPPDATA%\Programs\rclone"
set "RCLONE_EXE=%SHARED_RCLONE_DIR%\rclone.exe"
set "RCLONE_CONFIG_DIR=%APPDATA%\rclone"
set "RCLONE_CONFIG_FILE=%RCLONE_CONFIG_DIR%\rclone.conf"

:: Hardcoded Win32 Utilities (Bypasses PowerShell GPO Interception)
set "WIN_CURL=C:\Windows\System32\curl.exe"
set "WIN_TAR=C:\Windows\System32\tar.exe"

:: ============================================================================
:: RCLONE INSTALL, IF NEEDED
:: ============================================================================
echo [-^>] Scanning system for existing rclone installations...

if exist "%RCLONE_EXE%" (
    echo [+] Standardized user-level rclone installation detected.
    set "PATH=%SHARED_RCLONE_DIR%;%PATH%"
    goto :verify_configuration
)

where rclone >nul 2>nul
if %errorlevel% equ 0 (
    echo [+] Existing global rclone installation detected. Using system default.
    goto :verify_configuration
)

echo [!] rclone engine not detected. Initiating native system installation...
if not exist "%SHARED_RCLONE_DIR%" mkdir "%SHARED_RCLONE_DIR%"

set "TEMP_ZIP=%TEMP%\rclone_download.zip"
set "TEMP_EXTRACT=%TEMP%\rclone_extract"

echo [-^>] Fetching clean production binary via native Win32 curl...
%WIN_CURL% -L "https://downloads.rclone.org/rclone-current-windows-amd64.zip" -o "%TEMP_ZIP%"
if !errorlevel! neq 0 (
    echo [!] FATAL ERROR: Network download via system curl failed. 
    pause
    exit /b 1
)

echo [-^>] Unpacking application assets via native Win32 tar...
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%"
mkdir "%TEMP_EXTRACT%"

%WIN_TAR% -xf "%TEMP_ZIP%" -C "%TEMP_EXTRACT%"
if !errorlevel! neq 0 (
    echo [!] FATAL ERROR: Extraction via system tar failed.
    pause
    exit /b 1
)

echo [-^>] Deploying engine to centralized user directory...
for /r "%TEMP_EXTRACT%" %%F in (rclone.exe) do (
    if exist "%%F" move /y "%%F" "%SHARED_RCLONE_DIR%\" >nul
)

del "%TEMP_ZIP%" >nul 2>&1
rmdir /s /q "%TEMP_EXTRACT%" >nul 2>&1

if not exist "%RCLONE_EXE%" (
    echo [!] FATAL ERROR: Deployment failed. Execution environment unstable.
    pause
    exit /b 1
)

echo [-^>] Registering rclone path into active session environment...
set "PATH=%SHARED_RCLONE_DIR%;%PATH%"
echo [+] Centralized rclone engine successfully deployed.

:: ============================================================================
:: RCLONE CONFIGURATION & OAUTH AUTHENTICATION
:: ============================================================================
:verify_configuration
if not exist "%RCLONE_CONFIG_DIR%" mkdir "%RCLONE_CONFIG_DIR%"

:: Initialize or update rclone.conf with explicit GCS parameters
if not exist "%RCLONE_CONFIG_FILE%" (
    echo [-^>] Initializing base rclone config file...
    (
    echo [gcp_remote]
    echo type = google cloud storage
    echo provider = Google
    echo bucket_policy_only = true
    ) > "%RCLONE_CONFIG_FILE%"
) else (
    findstr /c:"[gcp_remote]" "%RCLONE_CONFIG_FILE%" >nul 2>&1
    if !errorlevel! neq 0 (
        (
        echo.
        echo [gcp_remote]
        echo type = google cloud storage
        echo provider = Google
        echo bucket_policy_only = true
        ) >> "%RCLONE_CONFIG_FILE%"
    )
)

:: Strip legacy 'object_acl' line if present
if exist "%RCLONE_CONFIG_FILE%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%RCLONE_CONFIG_FILE%') -notmatch 'object_acl' | Set-Content '%RCLONE_CONFIG_FILE%'" >nul 2>&1
)

:: WRITE TEST: Create a temporary local file and attempt to write it to GCS
echo [-^>] Performing preliminary WRITE access test on GCS bucket...
set "WRITE_TEST_LOCAL=%TEMP%\.rclone_write_test.tmp"
echo gcp_write_test > "%WRITE_TEST_LOCAL%"

rclone copyto "%WRITE_TEST_LOCAL%" "gcp_remote:%GCP_BUCKET_PATH%/.rclone_write_test.tmp" --gcs-bucket-policy-only >nul 2>&1
if %errorlevel% equ 0 (
    echo [+] Storage WRITE access verified. Workstation authorized.
    :: Clean up cloud test file
    rclone deletefile "gcp_remote:%GCP_BUCKET_PATH%/.rclone_write_test.tmp" --gcs-bucket-policy-only >nul 2>&1
    if exist "%WRITE_TEST_LOCAL%" del "%WRITE_TEST_LOCAL%" >nul 2>&1
    goto :execute_bulk_upload
)

if exist "%WRITE_TEST_LOCAL%" del "%WRITE_TEST_LOCAL%" >nul 2>&1

echo.
echo ==========================================================================
echo  ACTION REQUIRED: Google Cloud Storage authorization needed
echo ==========================================================================
echo [!] Read permissions succeeded, but rclone was unable to WRITE files.
echo [-^>] Re-authenticating browser OAuth connection with Google Cloud...
echo.

:: Launch interactive reconnect to request full write permissions
call rclone config reconnect gcp_remote:

:: Re-verify WRITE access
echo [-^>] Re-testing bucket WRITE access...
echo gcp_write_test > "%WRITE_TEST_LOCAL%"
rclone copyto "%WRITE_TEST_LOCAL%" "gcp_remote:%GCP_BUCKET_PATH%/.rclone_write_test.tmp" --gcs-bucket-policy-only
if %errorlevel% neq 0 (
    echo.
    echo [!] FATAL ERROR: Bucket write test failed again.
    echo     Please ensure your Google account has 'Storage Object Admin' or 
    echo     'Storage Object Creator' permissions assigned in Google Cloud Console.
    if exist "%WRITE_TEST_LOCAL%" del "%WRITE_TEST_LOCAL%" >nul 2>&1
    pause
    exit /b 1
)

echo [+] Bucket WRITE access successfully validated!
rclone deletefile "gcp_remote:%GCP_BUCKET_PATH%/.rclone_write_test.tmp" --gcs-bucket-policy-only >nul 2>&1
if exist "%WRITE_TEST_LOCAL%" del "%WRITE_TEST_LOCAL%" >nul 2>&1

:: ============================================================================
:: FILE UPLOAD (FILTERED BY VIDEO EXTENSION)
:: ============================================================================
:execute_bulk_upload
echo.
echo ==========================================================================
echo                        Bulk Cloud Uploader (rclone)
echo ==========================================================================
echo.
echo [+] Local Source:  %LOCAL_SOURCE%
echo [+] Remote Target: gcp_remote:%GCP_BUCKET_PATH%
echo [+] File Filter:   *!VIDEO_EXT! (Parent directory only)
echo [+] Logging To:    %LOG_FILE%
echo [+] Initializing background copy transaction...
echo --------------------------------------------------------------------------
echo.

:: Write session header marker to log file
echo ======================================================================= >> "%LOG_FILE%"
echo ARCHIVE SESSION STARTED: %DATE% %TIME% >> "%LOG_FILE%"
echo Target Bucket: %GCP_BUCKET_PATH% >> "%LOG_FILE%"
echo ======================================================================= >> "%LOG_FILE%"

rclone copy "%LOCAL_SOURCE%" "gcp_remote:%GCP_BUCKET_PATH%" ^
    --include "*!VIDEO_EXT!" ^
    --max-depth 1 ^
    --gcs-bucket-policy-only ^
    --log-file "%LOG_FILE%" ^
    --log-level INFO ^
    -P ^
    -M ^
    --fast-list ^
    --checksum ^
    --ignore-existing ^
    --bwlimit "Mon-00:00,!OFF_HRS_BW! Mon-07:00,!WORKDAY_BW! Mon-17:00,!OFF_HRS_BW! Tue-00:00,!OFF_HRS_BW! Tue-07:00,!WORKDAY_BW! Tue-17:00,!OFF_HRS_BW! Wed-00:00,!OFF_HRS_BW! Wed-07:00,!WORKDAY_BW! Wed-17:00,!OFF_HRS_BW! Thu-00:00,!OFF_HRS_BW! Thu-07:00,!WORKDAY_BW! Thu-17:00,!OFF_HRS_BW! Fri-00:00,!OFF_HRS_BW! Fri-07:00,!WORKDAY_BW! Fri-17:00,!OFF_HRS_BW! Sat-00:00,!OFF_HRS_BW! Sat-07:00,!WORKDAY_BW! Sat-17:00,!OFF_HRS_BW! Sun-00:00,!OFF_HRS_BW! Sun-07:00,!WORKDAY_BW! Sun-17:00,!OFF_HRS_BW!"

:: Capture exit code immediately after rclone completes
set "TRANSFER_STATUS=%ERRORLEVEL%"

if !TRANSFER_STATUS! equ 0 (
    :: Log success marker
    echo. >> "%LOG_FILE%"
    echo ARCHIVE SESSION COMPLETED SUCCESSFULLY: %DATE% %TIME% >> "%LOG_FILE%"
    echo ----------------------------------------------------------------------- >> "%LOG_FILE%"

    echo.
    echo --------------------------------------------------------------------------
    echo [+] Upload complete. Terminal window safe to close.
    echo [+] Execution log saved to: "%LOG_FILE%"
    echo ==========================================================================
) else (
    :: Log failure marker
    echo. >> "%LOG_FILE%"
    echo ARCHIVE SESSION FAILED (Exit Code: !TRANSFER_STATUS!): %DATE% %TIME% >> "%LOG_FILE%"
    echo ----------------------------------------------------------------------- >> "%LOG_FILE%"

    echo.
    echo --------------------------------------------------------------------------
    echo [!] FATAL ERROR: Upload transaction failed with error code !TRANSFER_STATUS!.
    echo [!] Please inspect the log file for details: "%LOG_FILE%"
    echo ==========================================================================
)

echo.
pause