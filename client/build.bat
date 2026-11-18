@echo off
rem Budowanie ProfileManager.exe (Windows, Python 3.12+)
py -3.12 -m venv .venv || goto :err
call .venv\Scripts\activate || goto :err
pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --clean --onedir --windowed --name ProfileManager ^
  --collect-all playwright --collect-all qrcode --hidden-import PIL ^
  main.py || goto :err
echo.
echo Gotowe: dist\ProfileManager\ProfileManager.exe
exit /b 0
:err
echo Blad budowania.
exit /b 1
