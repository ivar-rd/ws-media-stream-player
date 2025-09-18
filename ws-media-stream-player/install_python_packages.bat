@echo off
REM ===============================
REM Install Python dependencies
REM ===============================

echo Installing Python packages from requirements.txt...
python -m pip install -r requirements.txt

echo.
echo Installation complete.
pause