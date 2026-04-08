@echo off
setlocal

:: 1. CHECK FOR VIRTUAL ENVIRONMENT
if exist ".venv\Scripts\python.exe" (
    echo [SKIP] Virtual environment already exists.
) else (
    echo [INFO] Creating Python Virtual Environment...
    python -m venv .venv
    echo [INFO] Environment created.
)

:: 2. ENSURE LIBRARIES ARE INSTALLED
:: We always run this because it quickly verifies that requirements are satisfied.
echo [INFO] Verifying Python requirements...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\pip install pandas PyYAML tqdm >nul
echo [INFO] Python libraries are ready.

:: 3. CHECK FOR FFMPEG
if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [SKIP] FFmpeg already exists.
) else (
    echo [INFO] FFmpeg not found. Downloading portable version...
    
    :: Use curl to download
    curl -L -o ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
    
    echo [INFO] Extracting FFmpeg...
    tar -xf ffmpeg.zip
    
    :: Find any folder that contains a 'bin' subfolder and move it to 'ffmpeg'
    for /r %%d in (bin) do (
        if exist "%%d\ffmpeg.exe" (
            set "found_path=%%~dpd"
            :: Remove the trailing backslash
            setlocal enabledelayedexpansion
            set "found_path=!found_path:~0,-1!"
            move "!found_path!" ffmpeg
            endlocal
            goto :cleanup
        )
    )

    :cleanup
    echo [INFO] Cleaning up download...
    del ffmpeg.zip
)

echo.
echo ==============================================
echo Setup Complete! 
echo You can run the script via process_videos.bat
echo ==============================================
pause