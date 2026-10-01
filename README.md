# lab-7c

Personal Windows utility.

Download the latest zip from **Releases**, unzip, run `helper.exe`. See `사용법.txt` inside the zip.

Build: `pyinstaller --onedir --noconsole --name helper --icon icon.ico --hidden-import mapsync --hidden-import tray --add-data "icon.ico;." --add-data "index.html;." --add-data "mapping.json;." --add-data "sions_cache.json;." ordr_helper.py`
