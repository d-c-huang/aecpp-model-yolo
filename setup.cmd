@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "setup_check=0"
set "setup_exit=0"
if /i "%~1"=="--check" set "setup_check=1"
if /i "%~1"=="--help" goto help
if not "%~1"=="" if /i not "%~1"=="--check" goto bad_argument

echo Yingge - Windows environment setup
echo Project: "%CD%"
echo.
if exist ".venv\Scripts\python.exe" goto validate_environment
if "%setup_check%"=="1" (
    echo [ERROR] No local virtual environment. Run setup.cmd first.
    set "setup_exit=1"
    goto finish
)

echo [1/4] Looking for 64-bit Python 3.11 or newer with Tk...
py -3 -c "import sys, struct, tkinter; assert sys.version_info >= (3, 11); assert struct.calcsize('P') == 8" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv ".venv"
    goto environment_created
)
python -c "import sys, struct, tkinter; assert sys.version_info >= (3, 11); assert struct.calcsize('P') == 8" >nul 2>&1
if not errorlevel 1 (
    python -m venv ".venv"
    goto environment_created
)
echo [ERROR] Compatible Python was not found.
echo Install 64-bit Python 3.11+ with Tcl/Tk and the Python launcher.
echo Enable "Add python.exe to PATH", then reopen this script.
echo Download: https://www.python.org/downloads/windows/
set "setup_exit=1"
goto finish

:environment_created
if errorlevel 1 (
    echo [ERROR] Could not create .venv. Check Python and folder permissions.
    echo An existing environment is never deleted automatically.
    set "setup_exit=1"
    goto finish
)

:validate_environment
echo [1/4] Checking the local Python and Tk...
".venv\Scripts\python.exe" -c "import sys, struct, tkinter; assert sys.version_info >= (3, 11), 'Python 3.11+ required'; assert struct.calcsize('P') == 8, '64-bit Python required'; tkinter.Tcl(); print(sys.version)"
if errorlevel 1 (
    echo [ERROR] The local environment is missing, incompatible, or broken.
    echo Do not copy .venv between computers. Rename it and run setup.cmd again.
    set "setup_exit=1"
    goto finish
)
if "%setup_check%"=="1" goto check_dependencies
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt is missing. Download the complete repository.
    set "setup_exit=1"
    goto finish
)

echo [2/4] Updating pip inside .venv...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip setup failed. Check the error above, network, and free space.
    echo Fix the cause, then run setup.cmd again.
    set "setup_exit=1"
    goto finish
)
echo [3/4] Installing requirements.txt. The first installation may take a while...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed; the environment is not ready.
    echo Check the pip error above and available disk space. Retry setup.cmd.
    echo Existing project data and models have not been removed.
    set "setup_exit=1"
    goto finish
)

:check_dependencies
echo [4/4] Checking application dependencies...
".venv\Scripts\python.exe" -c "import tkinter, av, PIL, yaml, scipy, yt_dlp, psutil, ultralytics, torch, bs4; print('Tk:', tkinter.TkVersion, 'PyTorch:', torch.__version__); print('CUDA available:', torch.cuda.is_available())"
if errorlevel 1 (
    echo [ERROR] A dependency could not be imported. See the error above.
    echo Run setup.cmd to install or repair the local requirements.
    set "setup_exit=1"
    goto finish
)
where ffmpeg.exe >nul 2>&1
if errorlevel 1 (
    echo [NOTICE] FFmpeg was not found on PATH.
    echo Local decoding uses PyAV. Online videos with separate audio/video
    echo streams need the FFmpeg executable to merge the downloaded streams.
    echo See README.md and https://ffmpeg.org/download.html
)
echo.
echo [OK] Python dependencies are ready. Double-click start.cmd to launch.
echo CPU learning is supported. A CUDA-capable PyTorch build is optional.
goto finish

:help
echo Usage: setup.cmd [--check]
echo Default: create .venv if needed and install requirements.txt.
echo --check: validate the existing environment without installing anything.
exit /b 0

:bad_argument
echo [ERROR] Unknown argument. Use setup.cmd or setup.cmd --check.
exit /b 2

:finish
if "%setup_check%"=="0" pause
exit /b %setup_exit%
