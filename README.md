# ws-media-stream-player

## FFMPEG Version
* 7.0.1 (2024-05-26)

##  Development for Windows
### Prerequisites
* Operating system: Windows
* Have [git](https://git-scm.com), [Python3.13](https://www.python.org) installed
* To build ws-media-stream-player, have packages [PyQt5](https://pypi.org/project/PyQt5/), [construct](https://pypi.org/project/construct/), [websocket-client](https://pypi.org/project/websocket-client/), [requests](https://pypi.org/project/requests/), and [pyinstaller](https://pypi.org/project/pyinstaller/)  installed

Install python packages with the following batch file:
```
install_python_packages.bat
```

### Build ws-media-stream-player:

Build:

```
cd ws-media-stream-player\ws-media-stream-player
make_win_x64.bat
```
The build results are located at ```ws-media-stream-player\ws-media-stream-player\outputs\windows-x86_64```
