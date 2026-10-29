@echo off
REM Compila DualSense One UI Studio a un .exe standalone (Windows)
REM Solo necesitas Python instalado con "Add python.exe to PATH".
REM Versiones FIJAS al build bueno de Pedro (2026-10-29): sin fijar,
REM pip baja lo mas nuevo del dia y dos PCs compilan programas distintos
REM (vgamepad 0.2.0 salio 2026-10-27 y rompio el cambio rapido por flechas).
REM pywin32 y pyinstaller quedan sin fijar: no difirieron entre builds
REM (pywin32 DLL 3.12.312.0 en el build bueno).

echo Instalando dependencias (Python 3.12)...
py -3.12 -m pip install customtkinter==6.0.0 hidapi==0.15.0 vgamepad==0.1.0 pystray==0.19.5 pillow==13.0.0 psutil==7.2.2 pywin32
py -3.12 -m pip install pyinstaller

echo.
echo Compilando...
py -3.12 -m PyInstaller --onefile --windowed --uac-admin --icon=valdomero.ico --name "DualSenseStudio" --collect-all customtkinter --collect-all vgamepad --hidden-import win32gui --hidden-import win32process --hidden-import win32api --hidden-import win32ui --hidden-import win32con --hidden-import pywintypes --hidden-import pythoncom --hidden-import psutil --add-data "valdomero.ico;." dualsense_studio.py
echo.
echo Copiando icono junto al .exe...
copy /Y valdomero.ico dist\ >nul
echo.
echo ============================================
echo  Listo. Tu .exe esta en la carpeta dist\
echo ============================================
pause
