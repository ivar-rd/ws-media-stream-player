:: generate vs_version for windows exe
set SERVICE_NAME=ws-media-stream-player

:: build windows exe
pyinstaller template_pyinstaller_win_x64.spec

:: copy windows exe
xcopy /e /s /y .\dist\"%SERVICE_NAME%".exe .\outputs\windows-x86_64\ 

:: copy import
xcopy /e /s /y .\src\import .\outputs\windows-x86_64\import\

:: remove the unused data
rmdir /s/q .\build
rmdir /s/q .\dist