@echo off
setlocal
set "config_file="

:: INITIAL SETUP
echo System configuration check:

:: Search for any installed version of Python
set "PYTHON_EXE="

:: Method A: Try the Windows Python Launcher (py)
py -3 --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXE=py -3"
    GOTO :VersionOK
)

py --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXE=py"
    GOTO :VersionOK
)

:: Method B: Check if 'python' is directly available on the system PATH
python --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXE=python"
    GOTO :VersionOK
)

:: Method C: Dynamically scan user-level directories for any installed Python version
IF EXIST "%LOCALAPPDATA%\Programs\Python" (
    FOR /D %%D IN ("%LOCALAPPDATA%\Programs\Python\Python*") DO (
        IF EXIST "%%D\python.exe" (
            set "PYTHON_EXE="%%D\python.exe""
            GOTO :VersionOK
        )
    )
)

:: If we reach here, no version of Python was detected on the system
GOTO :InstallPython

:InstallPython
echo.
echo ==========================================================
echo ERROR: Python was not found on your system.
echo ==========================================================
echo This application requires Python to run.
echo.
echo Please install Python using the website that is 
echo about to open.
echo.
echo NOTE: Checking the "Add Python to PATH" box during
echo installation is strongly recommended.
echo ==========================================================
echo.
echo Press any key to open the download page and exit...
pause >nul
start https://www.python.org/downloads/
exit /b

:VersionOK
echo [INFO] Python detected.

:: Check if the '.venv' folder already exists
IF NOT EXIST ".venv\" (
    echo.
    echo Compatible Python version found.
    echo First-time setup: Creating Python environment...
    
    :: Use the specifically located Python to create the environment
    %PYTHON_EXE% -m venv .venv
    
    echo Activating environment and downloading dependencies...
    echo ^(This may take several minutes. Do not close this window.^)
    call .venv\Scripts\activate.bat
    
    python -m pip install --upgrade pip >nul
    
    :: Install from requirements.txt
    pip install -r requirements.txt
    
    :: ERROR CATCHER: Halt if pip install fails
    IF %ERRORLEVEL% NEQ 0 (
        echo.
        echo ==========================================================
        echo ERROR: Failed to download and install dependencies!
        echo ==========================================================
        echo Removing broken environment...
        call .venv\Scripts\deactivate.bat >nul 2>&1
        cd /d "%~dp0"
        rmdir /s /q .venv
        echo.
        echo Please check your internet connection and try running this script again.
        pause
        exit /b
    )
) ELSE (
    echo [INFO] Environment found. Activating...
    call .venv\Scripts\activate.bat
)

:: ENSURE LIBRARIES ARE INSTALLED
:: We always run this because it quickly verifies that requirements are satisfied.
echo [INFO] Verifying Python requirements...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\pip install -r requirements.txt >nul

:: CHECK FOR FFMPEG
echo [INFO] Verifying FFmpeg installation...
if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [SKIP] FFmpeg installation found.
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

echo [INFO] Setup verification complete!
echo.

:: PROCESS VIDEOS
echo -----------------------------------------------------------
echo SEFIS Video Utility
echo -----------------------------------------------------------

:: 1. Show the instruction Message Box first
:: [void] suppresses the numeric return value of the button click
powershell -noprofile -command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('Click OK to select the configuration YAML file to use.', 'SEFIS Video Utility', 'OK', 'Information')"

echo Attempting to open file explorer...

:: 2. Open the File Explorer Dialog
for /f "usebackq delims=" %%I in (`powershell -noprofile -command "Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.OpenFileDialog; $f.Filter = 'YAML Files (*.yml)|*.yml'; $f.InitialDirectory = '%CD%'; if($f.ShowDialog() -eq 'OK') { $f.FileName }"`) do set "config_file=%%I"

:: 3. Final check
if "%config_file%"=="" (
    echo ERROR: No configuration file provided. 
    pause
    exit /b
)

echo.
echo Processing: "%config_file%"
echo -----------------------------------------------------------

:: Execute using the hidden .venv folder
.venv\Scripts\python scripts\py\clip-and-stitch.py "%config_file%"

echo.
echo -----------------------------------------------------------
echo Processing finished.
pause