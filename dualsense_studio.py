"""
DualSense One UI Studio - version corregida.
Basado en el diseno de Gemini; correcciones aplicadas:

FIX 1: "Desactivado" ahora manda 0x00 (verificado: DSAdvance apaga con
       reporte en cero). Gemini usaba 0x05 sin verificar.
FIX 2: modo 0x06 (vibracion) usa orden [fuerza, inicio, fin, frecuencia]
       segun DSAdvance (r57zone). Gemini lo tenia como [freq, ...].
FIX 3 (v2): HidHide por IOCTL real, verificado en el codigo fuente de
       nefarius/HidHide (Shared/HidHideIoctlContract.h + DEVELOPER.md).
       El toggle ahora SI configura el cloak: session blacklist (2056/2057),
       pensado exactamente para feeder apps como esta. Vive en memoria del
       kernel (no toca el registro) y se limpia solo al salir del proceso.
       Requiere driver instalado y probablemente ejecutar como admin.
FIX 4: ViGEmBus ausente ya no crashea: avisa con link de descarga y cae
       a modo "Solo Triggers".
FIX 5: Rumble de juegos via register_notification (API real de vgamepad).
       get_state() no existe; el try/except lo ocultaba y el rumble
       quedaba en 0 para siempre.
FIX 6: El loop mandaba ~250 paquetes HID/seg. Ahora solo escribe cuando
       algo cambio (mas heartbeat de 2.5s).
FIX 7: Chequeo de dependencias al arrancar con mensaje claro de que
       instalar (no mas crash mudo por import).
FIX 8: "Iniciar con Windows" inteligente + arranque al tray (--tray).
       .exe con --uac-admin (pide admin siempre). Si la app corre elevada,
       el autostart crea tarea programada (logon + highest): arranca
       elevada SIN prompt UAC. Si no, clave Run de HKCU.
FIX 9: bateria del mando en la barra superior (byte off+52, formula
       pydualsense) + color de luz DESACOPLADO del perfil: boton Color
       con picker, se guarda en studio_settings.json y ya no cambia al
       cambiar de perfil.
FIX 10: perfiles_dualsense.json y studio_settings.json ahora viven junto
        al .exe/.py (antes: directorio de trabajo -> con la tarea
        programada caian en System32).
FIX 11 (corregido por hardware): el byte 1 de 0x06 es FRECUENCIA de vibración
        (validado por Pedro: el slider "dureza" la controla). El slider se
        renombró a "Frecuencia de Vibración" / "Velocidad de Ráfaga" según
        el modo. El byte de DUREZA real en 0x06 sigue sin encontrarse.
        Solo quedan los sliders necesarios (adiós sliders de caza).
FIX 12: caza del byte de DUREZA real: Personalizado expone los bytes
        desconocidos B4..B10 como sliders "Caza" (los conocidos van con su
        nombre correcto: Frecuencia/Incio/Fin). Pedro caza uno a la vez y
        reporta cuál responde; luego se cablea como dureza.
FIX 13: pulseOptions del lightbar era 0x02 (FadeOut) y apagaba el color.
        Ahora 0x00 (Off, default verificado de pydualsense) = color solido.

Requiere en Windows: Python 3.10+, ViGEmBus driver, y:
    pip install -r requirements.txt
Opcional: HidHide (anti doble-input), configurado en su propia app.
"""

import os
import sys
import json
import time
import zlib
import ctypes
import threading
import subprocess
import re

APP_VERSION = "v2.7"
# Etiqueta de esta compilación (se muestra en el título): se sube en cada
# entrega para saber qué build está corriendo.
BUILD_TAG = "ota-v1"

# Actualizaciones OTA (GitHub Releases). El tag del Release (v0.1.0, v0.1.1...)
# se compara con OTA_VERSION: hay que subir OTA_VERSION en cada entrega.
OTA_VERSION = "0.1.0"
OTA_REPO = "dilannio1/DualSenseStudio"
OTA_ASSET_NAME = "DualSenseStudio.exe"
OTA_RELEASES_URL = "https://api.github.com/repos/dilannio1/DualSenseStudio/releases/latest"
OTA_IDLE_SECONDS = 5.0
OTA_SNOOZE_SECONDS = 24 * 3600


def _ota_parse_version(tag):
    """'v0.1.1' -> (0, 1, 1). None si el tag no es numérico."""
    try:
        t = str(tag).strip()
        if t.lower().startswith("v"):
            t = t[1:]
        return tuple(int(p) for p in t.split("."))
    except Exception:
        return None


def _ota_is_newer(latest_tag, current):
    """True si latest_tag es una versión mayor que current."""
    lat = _ota_parse_version(latest_tag)
    cur = _ota_parse_version(current)
    if lat is None or cur is None:
        return False
    n = max(len(lat), len(cur))
    lat += (0,) * (n - len(lat))
    cur += (0,) * (n - len(cur))
    return lat > cur


def _ota_snooze_active(until, version, tag, now):
    """True si el 'Después' sigue vigente para ESTA versión."""
    try:
        return bool(until) and float(until) > now and version == tag
    except Exception:
        return False


def _ota_updater_script(exe_path, new_path, proc_name):
    """Script .bat que espera a que la app cierre, reemplaza el .exe
    (reintenta hasta 30s por si el archivo sigue bloqueado), relanza
    la app y se borra solo."""
    lines = [
        "@echo off",
        "REM Actualizador de DualSense Studio: reemplaza el exe y relanza.",
        "set \"intentos=0\"",
        ":espera",
        f'tasklist /FI "IMAGENAME eq {proc_name}" 2>nul | find /I "{proc_name}" >nul',
        "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto espera )",
        ":reemplazo",
        f'move /Y "{new_path}" "{exe_path}" >nul 2>&1',
        f'if exist "{new_path}" ( set /a intentos+=1 & if %intentos% GEQ 30 goto fin & timeout /t 1 /nobreak >nul & goto reemplazo )',
        ":fin",
        f'start "" "{exe_path}"',
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def _check_dependencies():
    """FIX 7: avisa que falta en vez de crashear por import."""
    needed = [
        ("customtkinter", "customtkinter"),
        ("hid", "hidapi"),
        ("vgamepad", "vgamepad"),
        ("pystray", "pystray"),
        ("PIL", "pillow"),
        ("win32gui", "pywin32"),
        ("win32process", "pywin32"),
        ("psutil", "psutil"),
    ]
    missing = []
    for mod, pip_name in needed:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        msg = ("Faltan librerias:\n  pip install " + " ".join(missing)
               + "\n\n(Tambien instala el driver ViGEmBus para la emulacion virtual)")
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror("Faltan dependencias", msg)
            r.destroy()
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)


_check_dependencies()

import customtkinter as ctk
import hid
import vgamepad as vg
import pystray
from PIL import Image, ImageDraw
# Detección de ventana al frente: imports estáticos para que PyInstaller
# los incluya sí o sí en el exe (antes vivían solo dentro de la función
# y el empaquetado los dejaba fuera o sin sus DLLs).
import win32gui
import win32process
import psutil

# Configuracion visual general estilo One UI (Modo Oscuro Elegante)
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

SONY_VID = 0x054C
DUALSENSE_PIDS = [0x0CE6, 0x0DF2]
# FIX 10: los JSON viven junto al .exe/.py, no en el directorio de trabajo
# (con el autostart programado el CWD es System32 -> los archivos caian ahi).
def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _app_dir()
PROFILES_FILE = os.path.join(BASE_DIR, "perfiles_dualsense.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "studio_settings.json")
GAME_PROFILES_FILE = os.path.join(BASE_DIR, "juegos_perfiles.json")
GAME_ICONS_DIR = os.path.join(BASE_DIR, "game_icons")

def _find_valdomero_ico():
    """Busca valdomero.ico junto al .exe/.py o dentro del bundle PyInstaller (_MEIPASS)."""
    candidates = []
    try:
        candidates.append(BASE_DIR)
    except Exception:
        pass
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(meipass)
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    for d in candidates:
        p = os.path.join(d, "valdomero.ico")
        if os.path.isfile(p):
            return p
    return None
VIGEMBUS_URL = "https://github.com/nefarius/ViGEmBus/releases"
HIDHIDE_URL = "https://github.com/nefarius/HidHide/releases"

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010

TRIGGER_MODES_LIST = [
    "Desactivado",
    "Disparo de Arma (Clic Fuerte)",
    "Disparo Automático (Max Ready)",
    "Resistencia Continua",
    "Personalizado (Avanzado)"
]

# Cambio rápido dentro del juego: flecha -> perfil (decidido con Pedro).
# Solo gatillos (nunca emulación ni cloak, y nada más que los gatillos).
QUICK_SWITCH_MAP = {
    "up": "Disparo automatico",
    "down": "Gatillos Suaves (Estándar)",
    "left": "Simulador Carreras (Freno + Motor)",
    "right": "Disparo manual",
}

# Ventana del cambio rápido con un dedo: tras soltar el modificador sin
# flecha, la app espera este tiempo a que llegue la flecha antes de
# reenviar el toque pendiente (3 segundos, pedido por Pedro).
QUICK_SWITCH_WINDOW = 3.0

# Fábrica de sticks: sin zona muerta ni curva añadidas (passthrough).
# Solo vale lo que el usuario mueva y guarde (sticks_customized);
# los valores viejos guardados sin esa marca se ignoran en carga.
FACTORY_DEADZONE_L = 0
FACTORY_DEADZONE_R = 0
FACTORY_CURVE = 1.0

DEFAULT_PROFILES = {
    "Disparo manual": {
        "emulation": "Xbox 360 (XInput)",
        "l2_mode": "Disparo de Arma (Clic Fuerte)",
        "l2_start": 35, "l2_end": 85, "l2_force": 255, "l2_freq": 0,
        "r2_mode": "Disparo de Arma (Clic Fuerte)",
        "r2_start": 40, "r2_end": 90, "r2_force": 255, "r2_freq": 0,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": True, "mouse_sens": 1.5,
        "hidhide_active": True,
        "lightbar": [0, 130, 255]
    },
    "Disparo automatico": {
        "emulation": "Xbox 360 (XInput)",
        "l2_mode": "Disparo de Arma (Clic Fuerte)",
        "l2_start": 35, "l2_end": 85, "l2_force": 255, "l2_freq": 0,
        "r2_mode": "Disparo Automático (Max Ready)",
        "r2_start": 0, "r2_end": 255, "r2_force": 15, "r2_freq": 16,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": True, "mouse_sens": 1.5,
        "hidhide_active": True,
        "lightbar": [255, 60, 0]
    },
    "Simulador Carreras (Freno + Motor)": {
        "emulation": "Xbox 360 (XInput)",
        "l2_mode": "Resistencia Continua",
        "l2_start": 10, "l2_end": 255, "l2_force": 240, "l2_freq": 0,
        "r2_mode": "Personalizado (Avanzado)",
        "r2_start": 1, "r2_end": 37, "r2_force": 100, "r2_freq": 35,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": False, "mouse_sens": 1.5,
        "hidhide_active": True,
        "lightbar": [255, 140, 0]
    },
    "Gatillos Suaves (Estándar)": {
        "emulation": "Xbox 360 (XInput)",
        "l2_mode": "Desactivado",
        "l2_start": 0, "l2_end": 0, "l2_force": 0, "l2_freq": 0,
        "r2_mode": "Desactivado",
        "r2_start": 0, "r2_end": 0, "r2_force": 0, "r2_freq": 0,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": True, "mouse_sens": 1.5,
        "hidhide_active": False,
        "lightbar": [255, 255, 255]
    },
    "Anti-cheat": {
        "emulation": "Solo Triggers (Sin Emular)",
        "l2_mode": "Disparo de Arma (Clic Fuerte)",
        "l2_start": 35, "l2_end": 85, "l2_force": 255, "l2_freq": 0,
        "r2_mode": "Disparo Automático (Max Ready)",
        "r2_start": 0, "r2_end": 255, "r2_force": 15, "r2_freq": 16,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": True, "mouse_sens": 1.5,
        "hidhide_active": False,
        "applies_posture": True,
        "lightbar": [128, 0, 255]
    },
    "Anti-cheat - Gatillos apagados": {
        "emulation": "Solo Triggers (Sin Emular)",
        "l2_mode": "Desactivado",
        "l2_start": 0, "l2_end": 0, "l2_force": 0, "l2_freq": 0,
        "r2_mode": "Desactivado",
        "r2_start": 0, "r2_end": 0, "r2_force": 0, "r2_freq": 0,
        "deadzone_l": 0, "deadzone_r": 0, "curve": 1.0,
        "touchpad_mouse": True, "mouse_sens": 1.5,
        "hidhide_active": False,
        "applies_posture": True,
        "lightbar": [255, 0, 128]
    }
}


def _ctl_code(device_type, function, method, access):
    # Macro CTL_CODE de Windows, verificada en Shared/HidHideIoctlContract.h
    return (device_type << 16) | (access << 14) | (function << 2) | method


# IOCTLs de HidHide (IoControlDeviceType=32769, METHOD_BUFFERED=0, FILE_READ_DATA=1)
# OJO: los IOCTL de *sesion* (2056 ADD / 2057 CLR) NO se usan: se agregaron
# al driver el 2026-04-28 ("Add process-lifetime (session) blacklist") y
# NINGUN instalador publicado los trae (ultimo release estable: v1.5.230,
# 2024-05-11). En un driver publicado el 2056 rebota aunque todo lo demas
# funcione. Se usa el mecanismo CLASICO (2050/2051), presente en todos los
# drivers publicados: blacklist permanente + SET_ACTIVE, con guardado y
# restauracion de la config previa del usuario al apagar el toggle/salir.
_IOCTL_GET_BLACKLIST = _ctl_code(32769, 2050, 0, 1)
_IOCTL_SET_BLACKLIST = _ctl_code(32769, 2051, 0, 1)
# Whitelist permanente (verificado en Shared/HidHideIoctlContract.h del
# repo nefarius/HidHide): 2048 = GET, 2049 = SET. Es persistente
# (queda en HKLM\SYSTEM\CurrentControlSet\Services\HidHide\Parameters),
# a diferencia del cloak de sesión, que vive solo en memoria del kernel.
_IOCTL_GET_WHITELIST = _ctl_code(32769, 2048, 0, 1)
_IOCTL_SET_WHITELIST = _ctl_code(32769, 2049, 0, 1)
_IOCTL_GET_ACTIVE = _ctl_code(32769, 2052, 0, 1)
_IOCTL_SET_ACTIVE = _ctl_code(32769, 2053, 0, 1)


class HidHideController:
    """Control real de HidHide via IOCTL (codigo verificado en nefarius/HidHide).

    Usa el mecanismo CLASICO: blacklist permanente (2050/2051) + SET_ACTIVE,
    el mismo del HidHide Client oficial y el unico presente en todos los
    drivers publicados (los IOCTL de sesion 2056/2057 no han salido en
    ningun instalador). La blacklist es persistente (registro), asi que
    enable() GUARDA la lista previa del usuario y disable() la RESTAURA
    tal cual, igual que el estado Active previo. Se ocultan TODAS las
    interfaces HID del mando (pad_instance_ids): los juegos pueden abrir
    una interfaz distinta a la que la app usa para los gatillos, y
    ocultar solo una deja el mando visible (fallo silencioso).
    Si la app muere a la fuerza (crash / terminar proceso) con el cloak puesto, la entrada
    queda: se quita reabriendo la app y apagando el toggle, o desde el
    HidHide Client oficial (ahi la entrada es visible).
    Requiere el driver instalado y (casi seguro) ejecutar como admin.
    """

    _DEVICE = r'\\.\HidHide'

    _last_open_error = None  # último error Windows de CreateFileW (diagnóstico)

    def __init__(self):
        self._prev_active = None  # para restaurar la config del usuario al salir
        self._prev_blacklist = None  # lista previa (sin tocar) para restaurar

    @classmethod
    def service_state(cls):
        """Estado del servicio HidHide. Retorna (existe, estado:int|None).
        Parseo NUMERICO del STATE (1=STOPPED, 4=RUNNING): a prueba del idioma
        de Windows (en espanol sc muestra ESTADO/DETENIDO, no RUNNING/STOPPED,
        asi que buscar esas palabras inglesas falla siempre)."""
        try:
            r = subprocess.run("sc query HidHide", shell=True,
                               capture_output=True, text=True, timeout=10)
            out = r.stdout + r.stderr
        except Exception:
            return False, None
        m = re.search(r"^\s*(STATE|ESTADO)\s*:\s*(\d+)", out,
                      re.MULTILINE | re.IGNORECASE)
        if m:
            return True, int(m.group(2))
        if "1060" in out or r.returncode == 1060:
            return False, None
        if r.returncode == 0:
            return True, None
        return False, None

    @classmethod
    def ensure_service_running(cls, timeout_s=4.0):
        """Si el servicio esta detenido, lo arranca y espera a RUNNING.
        El driver es DEMAND_START (ver HidHide.inf): sin el Client ni el
        Watchdog, nadie lo arranca y el dispositivo de HidHide no existe.
        Retorna (ok, mensaje)."""
        exists, state = cls.service_state()
        if not exists:
            return False, "HidHide no está instalado (el servicio no existe)"
        if state == 4:
            return True, "servicio en ejecución"
        started = False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            _, state = cls.service_state()
            if state == 4:
                return True, "servicio arrancado"
            if state in (1, None) and not started:
                started = True
                try:
                    r = subprocess.run("sc start HidHide", shell=True,
                                       capture_output=True, text=True, timeout=10)
                    start_out = r.stdout + r.stderr
                except Exception as e:
                    return False, f"no se pudo ejecutar sc start ({e})"
                if "1058" in start_out:
                    return False, ("el servicio HidHide está DESHABILITADO: "
                                   "actívalo en services.msc o abre el HidHide Client una vez")
            time.sleep(0.25)
        return False, ("el servicio HidHide no llegó a RUNNING: "
                       "revisa services.msc o abre el HidHide Client una vez")

    @staticmethod
    def is_installed():
        """Compat: True si el servicio HidHide existe (cualquier estado)."""
        exists, _ = HidHideController.service_state()
        return exists

    @staticmethod
    def is_admin():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @classmethod
    def _open(cls):
        k32 = ctypes.windll.kernel32
        try:
            # Tipos correctos: sin restype, ctypes trunca el HANDLE a 32 bits.
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
            k32.GetLastError.restype = ctypes.c_ulong
        except Exception:
            pass
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        OPEN_EXISTING = 3
        h = k32.CreateFileW(cls._DEVICE, GENERIC_READ | GENERIC_WRITE,
                            0, None, OPEN_EXISTING, 0, None)
        if h is None or h == -1 or h == 0xFFFFFFFFFFFFFFFF:  # INVALID_HANDLE_VALUE
            try:
                cls._last_open_error = k32.GetLastError()
            except Exception:
                cls._last_open_error = None
            return None
        cls._last_open_error = 0
        return h

    @classmethod
    def _close(cls, h):
        try:
            k32 = ctypes.windll.kernel32
            try:
                k32.CloseHandle.restype = ctypes.c_int
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
            except Exception:
                pass
            k32.CloseHandle(h)
        except Exception:
            pass

    @classmethod
    def _ioctl(cls, code, in_bytes=b'', out_size=64):
        """Manda un IOCTL al driver. Retorna (ok, bytes_de_respuesta)."""
        h = cls._open()
        if h is None:
            return False, b''
        try:
            k32 = ctypes.windll.kernel32
            try:
                k32.DeviceIoControl.restype = ctypes.c_int
                k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
            except Exception:
                pass
            in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
            # El driver valida tamaños EXACTOS: GET_ACTIVE exige salida de 1
            # byte; los SET/ADD/CLR exigen salida 0. Pasar 64 (default viejo)
            # hacía que el driver rechazara TODOS los IOCTL con
            # STATUS_INVALID_PARAMETER: esa era la causa del cloak.
            out_buf = ctypes.create_string_buffer(out_size) if out_size > 0 else None
            returned = ctypes.c_ulong(0)
            ok = k32.DeviceIoControl(h, code,
                                     in_buf, len(in_bytes) if in_bytes else 0,
                                     out_buf, out_size, ctypes.byref(returned), None)
            if out_buf is not None:
                return bool(ok), bytes(out_buf.raw[:returned.value])
            return bool(ok), b''
        finally:
            cls._close(h)

    @classmethod
    def get_active(cls):
        ok, out = cls._ioctl(_IOCTL_GET_ACTIVE, out_size=1)
        if not ok or not out:
            return None
        return bool(out[0])

    @classmethod
    def set_active(cls, on):
        # Payload: un BOOLEAN de 1 byte (ver FilterDriverProxy.cpp)
        ok, _ = cls._ioctl(_IOCTL_SET_ACTIVE, b'\x01' if on else b'\x00', out_size=0)
        return ok

    @staticmethod
    def instance_id_from_hid_path(hid_path):
        """Convierte un device interface path (hidapi) a device instance
        path (formato SetupDiGetDeviceInstanceId que pide HidHide).
        USB: \\\\?\\hid#vid_054c&pid_0ce6&mi_03#7&...#{guid} ->
             HID\\VID_054C&PID_0CE6&MI_03\\7&...
        BT:  \\\\?\\HID#{guid-servicio}_VID&..._PID&...#c&...#{guid} ->
             HID\\{guid-servicio}_VID&..._PID&...\\C&...
        El GUID de interfaz va SIEMPRE al final: se corta en el ULTIMO
        '#{', no en el primero. El camino BT lleva otro '#{' al
        principio (HID#{guid}...): cortar en el primero dejaba solo
        'HID', que no corresponde a ningun dispositivo (fallo
        silencioso de v3/v4: los IOCTL pasaban y la relectura
        'verificaba' la misma basura)."""
        if isinstance(hid_path, (bytes, bytearray)):
            hid_path = bytes(hid_path).decode('utf-8', 'replace')
        p = hid_path
        if p.startswith('\\\\?\\'):
            p = p[4:]
        p = p.rsplit('#{', 1)[0]  # quita el GUID de interfaz (el ultimo)
        return p.replace('#', '\\').upper()

    @classmethod
    def pad_instance_ids(cls, primary_hid_path):
        """TODAS las instance IDs HID del mando, no solo la interfaz que
        abre la app: hid.enumerate() puede listar varias interfaces del
        DualSense y los juegos pueden abrir otra distinta a la nuestra.
        Ocultar solo una = el juego sigue viendo el mando (fallo
        silencioso: los IOCTL pasan y la relectura verifica, pero el
        juego abre otro PDO). Sin duplicados, la primaria primero. Si
        enumerar falla, devuelve solo la primaria."""
        ids = []
        try:
            primary = cls.instance_id_from_hid_path(primary_hid_path) if primary_hid_path else None
        except Exception:
            primary = None
        if primary and 'VID' in primary:
            ids.append(primary)
        try:
            for dev in hid.enumerate(SONY_VID):
                if dev.get('product_id') in DUALSENSE_PIDS:
                    try:
                        iid = cls.instance_id_from_hid_path(dev.get('path', ''))
                    except Exception:
                        continue
                    if iid and 'VID' in iid and not any(i.lower() == iid.lower() for i in ids):
                        ids.append(iid)
        except Exception:
            pass
        return ids

    @staticmethod
    def _cloak_debug(msg):
        """Diagnostico post-hoc: escribe en debug_hid.log (mismo archivo
        que el resto de la app). Nunca en el camino de entrada."""
        try:
            with open(os.path.join(BASE_DIR, "debug_hid.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} CLOAK {msg}\n")
        except Exception:
            pass

    @classmethod
    def _multisz(cls, strings):
        # MULTI_SZ: strings UTF-16LE null-terminados + null final extra.
        # El tamaño debe ser par e incluir todos los terminadores.
        return b''.join(s.encode('utf-16-le') + b'\x00\x00' for s in strings) + b'\x00\x00'

    @classmethod
    def _dos_device_path(cls, path):
        """Convierte C:\\...\\app.exe a \\Device\\HarddiskVolumeN\\...\\app.exe
        (formato que el driver compara), igual que el cliente oficial
        (QueryDosDeviceW, ver HidHideCLI/src/Volume.cpp)."""
        try:
            ap = os.path.abspath(path)
            drive = os.path.splitdrive(ap)[0]
            if not drive:
                return None
            buf = ctypes.create_unicode_buffer(512)
            if ctypes.windll.kernel32.QueryDosDeviceW(drive, buf, 512) == 0:
                return None
            return buf.value + ap[len(drive):]
        except Exception:
            return None

    @classmethod
    def _open_error_text(cls):
        """Texto del ultimo error de apertura del driver (diagnostico)."""
        err = getattr(cls, "_last_open_error", None)
        if not err:
            return "el dispositivo abrió pero el IOCTL falló"
        hint = {32: "otro programa tiene el dispositivo abierto",
                5: "permisos",
                2: "dispositivo no existe"}.get(err, "")
        return f"error Windows {err}" + (f": {hint}" if hint else "")

    @classmethod
    def _get_whitelist_detailed(cls):
        """(ok, lista). ok=False distingue 'fallo el IOCTL' de 'lista vacia'."""
        ok, out = cls._ioctl(_IOCTL_GET_WHITELIST, out_size=8192)
        if not ok or not out:
            return False, []
        try:
            text = out.decode('utf-16-le', errors='ignore')
        except Exception:
            return False, []
        return True, [s for s in text.split('\x00') if s]

    @classmethod
    def get_whitelist(cls):
        """Lista actual de la whitelist (rutas en notación DOS device)."""
        _, lst = cls._get_whitelist_detailed()
        return lst

    @classmethod
    def set_whitelist(cls, paths):
        ok, _ = cls._ioctl(_IOCTL_SET_WHITELIST, cls._multisz(paths), out_size=0)
        return ok

    @classmethod
    def _get_blacklist_detailed(cls):
        """(ok, lista). Lista vacia legitima -> (True, []): no se exige
        que la respuesta traiga bytes, solo que el IOCTL no falle."""
        ok, out = cls._ioctl(_IOCTL_GET_BLACKLIST, out_size=8192)
        if not ok:
            return False, []
        try:
            text = out.decode('utf-16-le', errors='ignore')
        except Exception:
            return False, []
        return True, [s for s in text.split('\x00') if s]

    @classmethod
    def set_blacklist(cls, paths):
        # Lista vacia = entrada de largo 0: el driver la interpreta como
        # "limpiar la lista" (ver OnControlDeviceIoSetBlacklist).
        in_bytes = cls._multisz(paths) if paths else b''
        ok, _ = cls._ioctl(_IOCTL_SET_BLACKLIST, in_bytes, out_size=0)
        return ok

    @classmethod
    def ensure_self_whitelisted(cls):
        """Agrega el .exe propio a la whitelist permanente y VERIFICA que quedo.
        Retorna (ok, motivo).
        Solo con el .exe compilado: con el .py el ejecutable seria python.exe
        y agregarlo eximiria a TODOS los programas Python, no solo a esta app.
        ORDEN: llamarse ANTES de activar el cloak. El driver no exime al dueno
        de la sesion (Logic.c: solo la whitelist exime): sin whitelist, la app
        no puede reabrir el mando tras activar el cloak."""
        if not getattr(sys, 'frozen', False):
            return False, "no compilado (.py): el whitelist no aplica"
        dev_path = cls._dos_device_path(sys.executable)
        if not dev_path:
            return False, "no se pudo convertir la ruta del .exe (QueryDosDeviceW)"
        ok, current = cls._get_whitelist_detailed()
        if not ok:
            return False, f"no se pudo leer la whitelist ({cls._open_error_text()})"
        if any(p.lower() == dev_path.lower() for p in current):
            return True, "ya estaba en la whitelist"
        if not cls.set_whitelist(current + [dev_path]):
            return False, f"no se pudo escribir la whitelist ({cls._open_error_text()})"
        ok2, current2 = cls._get_whitelist_detailed()
        if not ok2:
            return False, f"se escribió pero no se pudo verificar ({cls._open_error_text()})"
        if any(p.lower() == dev_path.lower() for p in current2):
            return True, "agregado y verificado en la whitelist"
        return False, "la escritura no quedó reflejada en la whitelist"

    def enable(self, instance_ids):
        """Activa el cloak para el control. Retorna (ok, mensaje).
        instance_ids: lista de TODAS las interfaces HID del mando.
        ORDEN OBLIGATORIO (el driver no exime al dueno de la sesion):
        1) servicio en ejecucion, 2) whitelist propio VERIFICADO,
        3) Active=1, 4) interfaces en la blacklist (guardando la previa)."""
        svc_ok, svc_msg = self.ensure_service_running()
        if not svc_ok:
            return False, svc_msg
        wl_ok, wl_msg = self.ensure_self_whitelisted()
        if not wl_ok:
            return False, (f"whitelist: {wl_msg}. El cloak no se activó: "
                           "sin whitelist la app no puede reabrir el mando.")
        active = self.get_active()
        if active is None:
            return False, f"sin acceso al driver ({self._open_error_text()})"
        self._prev_active = active
        if not active and not self.set_active(True):
            return False, f"no se pudo activar el cloak ({self._open_error_text()})"
        if isinstance(instance_ids, str):
            instance_ids = [instance_ids]
        self._cloak_debug(f"enable ids={instance_ids}")
        ok_bl, prev_bl = self._get_blacklist_detailed()
        if not ok_bl:
            return False, f"no se pudo leer la blacklist ({self._open_error_text()})"
        self._cloak_debug(f"prev_blacklist={prev_bl} active={active}")
        # Limpieza: v3/v4 escribieron la entrada basura 'HID' (conversion
        # rota en BT); nunca es una entrada real, se descarta al guardar
        # y al restaurar para que no quede para siempre.
        prev_bl = [p for p in prev_bl if p.strip().upper() != 'HID']
        if self._prev_blacklist is None:
            self._prev_blacklist = prev_bl
        missing = [i for i in instance_ids
                   if not any(p.lower() == i.lower() for p in prev_bl)]
        if missing:
            new_bl = prev_bl + missing
            if not self.set_blacklist(new_bl):
                return False, f"no se pudo ocultar el mando ({self._open_error_text()})"
            ok_bl2, cur_bl = self._get_blacklist_detailed()
            self._cloak_debug(f"escrito={new_bl} relectura={cur_bl}")
            if not ok_bl2 or not all(any(p.lower() == i.lower() for p in cur_bl) for i in instance_ids):
                return False, "la blacklist no quedó aplicada"
        self._cloak_debug("enable OK")
        return True, "cloak activo: los juegos no ven el DualSense físico"

    def disable(self):
        """Restaura la blacklist y el estado Active previos del usuario."""
        prev = self._prev_blacklist
        self._prev_blacklist = None
        self._cloak_debug(f"disable restaura={prev}")
        if prev is not None:
            prev = [p for p in prev if p.strip().upper() != 'HID']
            self.set_blacklist(prev)
        if self._prev_active is not None and not self._prev_active:
            self.set_active(False)
        self._prev_active = None


class DualSenseHardware:
    def __init__(self):
        self.device = None
        self.is_bluetooth = False
        self.connected = False
        self.hid_path = None  # para el cloak de HidHide

    def connect(self):
        for dev in hid.enumerate(SONY_VID):
            if dev['product_id'] in DUALSENSE_PIDS:
                self.device = hid.device()
                self.device.open_path(dev['path'])
                self.device.set_nonblocking(1)
                self.hid_path = dev['path']

                time.sleep(0.05)
                sample = self.device.read(128)
                if sample and (sample[0] == 0x31 or len(sample) > 64):
                    self.is_bluetooth = True
                else:
                    self.is_bluetooth = (dev.get('interface_number', -1) == -1)

                self.connected = True
                return True
        return False

    def close(self):
        if self.device:
            try:
                self.send_packet({"mode": "Desactivado"}, {"mode": "Desactivado"}, [0, 0, 0], 0, 0)
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.connected = False

    @staticmethod
    def build_trigger_bytes(cfg):
        """11 bytes por gatillo: [modo, p1..p10].
        Modos verificados contra DSAdvance (r57zone, open source):
          0x00 = off (reporte en cero)
          0x01 = resistencia continua, params [inicio, fuerza]
          0x02 = muro/seccion, params [inicio, fin, fuerza]
          0x06 = vibracion/metralleta.
        MAPA REAL DE 0x06 (validado en hardware por Pedro):
          b[1] = frecuencia de vibración
          b[2] = FUERZA de cada vibración (amplitud)
          b[3] = posición del gatillo donde EMPIEZA a vibrar (0=inmediato)
          b[4] = 0x20 (requerido > 0 para activar)
          b[5]..b[10] = muertos (no hacen nada)
        El orden de params de 0x02/0x01 sigue el dialogo de DualSenseX
        (validado en hardware: F1 controla el inicio, invertido)."""
        b = [0] * 11
        mode = cfg.get("mode", "Desactivado")
        start = int(cfg.get("start", 0))
        end = int(cfg.get("end", 90))
        force = int(cfg.get("force", 255))

        if mode == "Desactivado":
            b[0] = 0x00  # FIX 1: era 0x05 sin verificar
        elif mode == "Disparo de Arma (Clic Fuerte)":
            b[0] = 0x02
            wall_start = max(15, min(140, start))
            wall_snap = min(200, wall_start + 45)
            b[1] = wall_start
            b[2] = wall_snap
            b[3] = 255
        elif mode == "Disparo Automático (Max Ready)":
            # b[1] = frecuencia (slider visible "Velocidad de Ráfaga")
            # b[2] = 255 (fuerza máxima, fijo sin mostrar)
            # b[3] = 0 (vibra desde el inicio, fijo sin mostrar)
            # b[4] = 0x20 (requerido > 0)
            b[0] = 0x06
            b[1] = force
            b[2] = 255
            b[3] = 0
            b[4] = 0x20
        elif mode == "Resistencia Continua":
            b[0] = 0x01
            b[1] = start
            b[2] = force
        elif mode == "Personalizado (Avanzado)":
            # MODO 0x06 SIEMPRE (sin fallback): cambiar a 0x01/0x02 y volver a 0x06
            # deja el control trabado (hay que reconectar). Con b[1]=0 no vibra pero el modo se mantiene.
            # MAPA REAL validado en hardware por Pedro:
            #   b[1] = frecuencia de vibración
            #   b[2] = FUERZA de cada vibración (amplitud)
            #   b[3] = posición del gatillo donde EMPIEZA a vibrar (0=inmediato, más alto=más presionado)
            #   b[4] = 0x20 (requerido > 0)
            #   b[5..10] = muertos (validados: no hacen nada)
            b[0] = 0x06
            b[1] = force
            b[2] = start
            b[3] = end
            b[4] = 0x20
        return b

    def send_packet(self, r2_cfg, l2_cfg, rgb, rumble_left=0, rumble_right=0):
        if not self.connected or not self.device:
            return

        r2_bytes = self.build_trigger_bytes(r2_cfg)
        l2_bytes = self.build_trigger_bytes(l2_cfg)
        common = bytearray(47)
        common[0] = 0xFF
        common[1] = 0x57  # FIX 16: era 0xF7; los bits extra 0x20|0x80 son indocumentados y podrian bloquear el lightbar
        common[2] = int(rumble_left)
        common[3] = int(rumble_right)

        for i in range(11):
            common[10 + i] = r2_bytes[i]
            common[21 + i] = l2_bytes[i]

        common[38] = 0x03  # ledOption = Both (valor de la librería funcional dualsense-controller)
        common[39] = 0x01  # FIX 19: LightbarMode.LIGHT_ON (0x01); sin esto el lightbar ignora el color (byte faltante)
        common[41] = 0x00  # FIX 13: era 0x02 (FadeOut) y apagaba el color; Off = color solido
        common[42] = 0x00  # FIX 18: brillo ALTO (0x00=high, 0x02=low); probar si el bajo no deja ver el cambio
        common[43] = 0x04
        common[44] = int(rgb[0])
        common[45] = int(rgb[1])
        common[46] = int(rgb[2])

        try:
            if not self.is_bluetooth:
                pkt = bytearray([0x02]) + common
                self.device.write(pkt)
            else:
                pkt = bytearray(78)
                pkt[0] = 0x31
                pkt[1] = 0x02
                pkt[2:49] = common

                crc_input = bytes([0xA2]) + bytes(pkt[:74])
                crc = zlib.crc32(crc_input) & 0xFFFFFFFF
                pkt[74] = crc & 0xFF
                pkt[75] = (crc >> 8) & 0xFF
                pkt[76] = (crc >> 16) & 0xFF
                pkt[77] = (crc >> 24) & 0xFF
                self.device.write(pkt)
        except Exception:
            pass

    def _report_dbg_write(self, msg):
        # Registro de reportes crudos (debug_reportes.log): siempre activo y
        # limitado para no inundar el disco. Solo diagnostico: no cambia
        # ningun comportamiento de lectura.
        try:
            if not getattr(self, "_rep_dbg_started", False):
                self._rep_dbg_started = True
                with open(os.path.join(BASE_DIR, "debug_reportes.log"), "a", encoding="utf-8") as f:
                    f.write(f"\n=== Reportes {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            with open(os.path.join(BASE_DIR, "debug_reportes.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def _report_dbg_reject(self, data, rid):
        st = getattr(self, "_rep_dbg", None)
        if st is None:
            st = self._rep_dbg = {"rej": 0, "empty": 0, "acc": 0, "fmt": None, "nodata": False}
        st["rej"] += 1
        n = st["rej"]
        head = bytes(data[:20]).hex(" ")
        if n <= 30 or n % 200 == 0:
            self._report_dbg_write(f"RECHAZADO n={n} vacios={st['empty']} len={len(data)} rid=0x{rid:02x} bytes={head}")

    def _report_dbg_accept(self, data, rid):
        st = getattr(self, "_rep_dbg", None)
        if st is None:
            st = self._rep_dbg = {"rej": 0, "empty": 0, "acc": 0, "fmt": None, "nodata": False}
        st["acc"] += 1
        fmt = (rid, len(data))
        if st["fmt"] != fmt or st["acc"] <= 3:
            st["fmt"] = fmt
            self._report_dbg_write(f"ACEPTADO n={st['acc']} rechazos={st['rej']} rid=0x{rid:02x} len={len(data)}")

    def read_state(self):
        if not self.connected or not self.device:
            return None
        try:
            data = self.device.read(128)
        except Exception:
            return None

        if not data:
            st = getattr(self, "_rep_dbg", None)
            if st is None:
                st = self._rep_dbg = {"rej": 0, "empty": 0, "acc": 0, "fmt": None, "nodata": False}
            st["empty"] += 1
            if st["acc"] == 0 and st["rej"] == 0 and st["empty"] == 500 and not st["nodata"]:
                st["nodata"] = True
                self._report_dbg_write("SIN DATOS: 500 lecturas vacias seguidas, el control no entrega reportes")
            return None

        rid = data[0]
        if rid == 0x01 and len(data) >= 10:
            off = 1
            full_report = (len(data) >= 40)
        elif rid == 0x31 and len(data) >= 45:
            off = 2
            full_report = True
        else:
            self._report_dbg_reject(data, rid)
            return None
        self._report_dbg_accept(data, rid)

        st = {
            "LX": data[off + 0], "LY": data[off + 1],
            "RX": data[off + 2], "RY": data[off + 3],
        }

        if full_report:
            st["L2"] = data[off + 4]
            st["R2"] = data[off + 5]
            b0, b1, b2 = data[off + 7], data[off + 8], data[off + 9]
        else:
            b0, b1, b2 = data[off + 4], data[off + 5], data[off + 6]
            st["L2"] = data[off + 7] if len(data) > off + 8 else 0
            st["R2"] = data[off + 8] if len(data) > off + 8 else 0

        hat = b0 & 0x0F
        st["up"] = hat in (0, 1, 7)
        st["right"] = hat in (1, 2, 3)
        st["down"] = hat in (3, 4, 5)
        st["left"] = hat in (5, 6, 7)

        st["square"] = bool(b0 & 0x10)
        st["cross"] = bool(b0 & 0x20)
        st["circle"] = bool(b0 & 0x40)
        st["triangle"] = bool(b0 & 0x80)

        st["L1"] = bool(b1 & 0x01)
        st["R1"] = bool(b1 & 0x02)
        st["share"] = bool(b1 & 0x10)
        st["options"] = bool(b1 & 0x20)
        st["L3"] = bool(b1 & 0x40)
        st["R3"] = bool(b1 & 0x80)

        st["ps"] = bool(b2 & 0x01)
        st["touch_btn"] = bool(b2 & 0x02)

        if full_report and len(data) > off + 36:
            t_info = data[off + 32]
            st["touch_active"] = ((t_info & 0x80) == 0)
            st["touch_x"] = ((data[off + 34] & 0x0F) << 8) | data[off + 33]
            st["touch_y"] = (data[off + 35] << 4) | ((data[off + 34] & 0xF0) >> 4)
        else:
            st["touch_active"] = False
            st["touch_x"] = 0
            st["touch_y"] = 0

        # FIX 9: bateria. Byte en off+52 (pydualsense lee states[53]
        # normalizado; BT trae 1 byte extra -> off+52 vale en ambas).
        if len(data) > off + 52:
            batt = data[off + 52]
            st["batt_level"] = min((batt & 0x0F) * 10 + 5, 100)
            st["batt_charging"] = ((batt & 0xF0) >> 4) == 0x1
        else:
            st["batt_level"] = None
            st["batt_charging"] = False

        return st


class DualSenseStudioApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"DualSense One UI Studio {APP_VERSION} [{BUILD_TAG}]")
        # Icono valdomero en la barra de tareas (antes: icono genérico de Tk,
        # un cuadrado blanco de puntas redondeadas).
        try:
            _ico = _find_valdomero_ico()
            if _ico:
                self.iconbitmap(_ico)
        except Exception:
            pass
        self.geometry("960x920")
        # Redimensionable + scroll (TV 4K con zoom 250%: 920 > 864 utiles).
        # minsize: el ancho no baja del diseno (evita recortar las tarjetas);
        # el alto si baja y el contenido scrollea.
        self.resizable(True, True)
        self.minsize(960, 540)

        self.hw = DualSenseHardware()
        self.hidcloak = HidHideController()
        self.virtual_pad = None
        self.emulation_type = "DS4"  # default PS4 (se confirma al crear el selector)
        self.running_thread = False
        self.tray_icon = None
        # FIX 5: rumble real via callback de vgamepad (0-255)
        self.rumble_large = 0
        self.rumble_small = 0
        # FIX 6: para no mandar paquetes HID identicos en loop
        self._last_sig = None

        self.last_touch_active = False
        self.last_tx = 0
        self.last_ty = 0
        self.last_touch_btn = False

        self.profiles = self.load_profiles()
        # Arranque SIN perfil activo (pedido por Pedro): no se aplica ningún
        # efecto hasta que él elija un perfil o abra un juego mapeado. Así un
        # juego sin mapear (o que no quiera mapear) no recibe ningún perfil.
        self.current_profile_name = None
        self._profile_active = False
        # FIX 9: color de luz propio, no atado al perfil
        self.lightbar_color = self.load_settings()
        self._last_batt_txt = ""
        # Auto-guardado y juego automático
        self._loading_profile = False
        self._sticks_dirty = False  # True si el usuario movió un slider de sticks/curva desde la carga
        # Cambio rápido de perfil dentro del juego (modificador + flecha):
        # el modificador depende de la emulación (X360 -> clic del pad,
        # DS4/Solo -> Options); se traga mientras está mantenido y el toque
        # solo sin flecha se reenvía como un toque normal al soltar.
        self._qs_prev_mod = False
        self._qs_prev_dirs = {"up": False, "down": False, "left": False, "right": False}
        self._qs_switched = False
        self._qs_suppress_dpad = False
        self._qs_inject = 0
        self._qs_window_until = 0.0  # fin de la ventana de 3 s (0 = cerrada)
        # Máquina de estados de juegos (ver _check_game_detection):
        # VIGILANCIA = _last_auto_game None (ningún juego aplicado)
        # APLICADO = _last_auto_game con exe (su perfil está puesto)
        # AUSENCIA = aplicado pero no al frente; _absence_since marca desde cuándo
        self._last_auto_game = None  # exe del juego que activó el perfil actual (None = manual/vigilancia)
        self._absence_since = None  # timestamp (time.time()) desde el que el juego aplicado no está al frente
        self._ABSENCE_TIMEOUT = 10.0  # segundos de ausencia para cerrar la sesión del popup
        self._REVERT_GRACE = 5.0  # segundos sin NINGÚN exe mapeado antes de soltar el perfil
        self._detect_game_streak = None  # antirrebote: exe candidato + conteo para cambiar hacia el juego
        self._detect_game_count = 0
        self._pending_popup_exe = None  # recuadro demorado 15s: exe pendiente
        self._pending_popup_target = None  # perfil asignado del pendiente
        self._pending_popup_due = None  # time.time() en que debe abrirse
        self._detect_dialog = None  # popup de juego detectado abierto (si hay)
        self._detect_resolved = set()  # exes ya aceptados/ignorados con ese juego al frente
        self._just_added_exe = None  # exe recién agregado: no preguntar al reaparecer tras Agregar
        self._pad_nav = None  # navegación por control activa: {dialog, move, accept, cancel}
        self._pad_nav_prev = None  # flancos de botones de navegación (worker)
        self._popup_session_exe = None  # exe de la sesión actual del popup
        self._popup_absent_since = None  # inicio de ausencia de la sesión (cierre a 10s)
        self.heartbeat_enabled = True  # siempre encendido (sin interruptor en la barra)
        self.debug_log_enabled = False
        self.game_profiles = self.load_game_profiles()  # {exe: {name, profile, icon}}

        self.build_ui()
        # Anti Doble-Input: arranca encendido. Lo cambia el interruptor
        # manual o un perfil con postura (Anti-cheat / creado por el usuario).
        self.hidhide_switch.select()
        # Ratón táctil GLOBAL (pedido por Pedro): arranca APAGADO salvo que
        # studio_settings.json diga que el usuario lo encendió; ningún perfil
        # lo cambia después (solo el interruptor manual).
        if getattr(self, "_settings_touchpad_mouse", False):
            self.touch_switch.select()
        # Perfil inicial: Gatillos Suaves (Estándar) desde el arranque.
        # Antes abría sin perfil seleccionado (el combo quedaba en blanco).
        if not self.current_profile_name:
            self.current_profile_name = "Gatillos Suaves (Estándar)"
        if self.current_profile_name in self.profiles:
            self.load_profile_to_ui(self.current_profile_name)
            self.profile_combo.set(self.current_profile_name)
        else:
            self.profile_combo.set("")
        self.setup_system_tray()

        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # Auto-conexión: intenta conectar al iniciar, y reintenta cada 5s hasta lograrlo
        self.after(1500, self._try_auto_connect)
        # Detección de juegos: revisa cada 1s (proceso activo o ventana al frente)
        self.after(1000, self._game_detect_tick)
        # Actualizaciones: primer chequeo a los 10s (en hilo, nunca frena la UI)
        self.after(10000, lambda: self._ota_check_in_background(False))

    def _game_detect_tick(self):
        """Tick periódico (hilo principal) para el perfil automático por juego."""
        try:
            self._check_game_detection()
        except Exception:
            pass
        try:
            self._ota_prompt_if_idle()
        except Exception:
            pass
        self.after(1000, self._game_detect_tick)

    def _try_auto_connect(self):
        """Monitor persistente: conecta al iniciar y reconecta si el mando se apaga/desconecta."""
        if not self.hw.connected:
            # No conectado: intentar conectar
            self.start_service()
        else:
            # Conectado: verificar que el dispositivo siga presente
            still_there = False
            try:
                for dev in hid.enumerate(SONY_VID):
                    if dev['path'] == self.hw.hid_path:
                        still_there = True
                        break
            except Exception:
                pass
            if not still_there:
                # El mando se apagó o desconectó: limpiar para reconectar en el próximo tick
                self.stop_service()
                self.status_label.configure(text="● Mando desconectado, buscando...", text_color="#fbbf24")
                self.connect_btn.configure(text="Conectar", fg_color="#3b82f6", hover_color="#2563eb")
        # Siempre reprogramar (cada 5s)
        self.after(5000, self._try_auto_connect)

    def _on_touch_switch_changed(self):
        """Ratón táctil GLOBAL: solo el interruptor lo cambia; se guarda
        en studio_settings.json, no en el perfil."""
        try:
            self.save_settings_to_disk()
        except Exception:
            pass

    def _maybe_autosave_profile(self):
        """Guardado EXPLÍCITO (pedido por Pedro 2026-10-27): mover sliders
        o cambiar modos NO guarda nada; solo el botón Guardar persiste.
        Los cambios siguen aplicando en vivo (el worker lee los sliders
        directo), pero se pierden al cambiar de perfil o cerrar si no se
        dio a Guardar. Se conserva el método para no tocar los llamadores."""
        return

    def _on_device_lost(self):
        """El worker detectó que el mando se desconectó físicamente."""
        if not self.hw.connected:
            self.stop_service()
            # El monitor _try_auto_connect lo reconectará cuando vuelva
            self.status_label.configure(text="● Mando desconectado, buscando...", text_color="#fbbf24")

    # FIX 5: callback real de vgamepad (antes: get_state(), no existe)
    def _on_rumble(self, client, target, large_motor, small_motor, led_number, user_data):
        self.rumble_large = large_motor
        self.rumble_small = small_motor

    def load_profiles(self):
        # Fusion: los de serie siempre presentes (asi aparecen los nuevos
        # Anti-cheat aunque el archivo guardado sea viejo); lo guardado
        # manda sobre cada nombre que ya exista. Copias para no mutar
        # DEFAULT_PROFILES al editar.
        merged = {k: dict(v) for k, v in DEFAULT_PROFILES.items()}
        if os.path.exists(PROFILES_FILE):
            try:
                with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    merged[k] = dict(v) if isinstance(v, dict) else v
            except Exception:
                pass
        return merged

    def _atomic_json_write(self, path, data):
        """Guarda JSON de forma atómica (temporal + renombre) y, si las
        copias están activadas en Ajustes, guarda antes una copia .bak del
        archivo anterior. Así un corte a mitad de guardado no deja el
        archivo corrupto ni se pierden los perfiles."""
        import shutil
        if getattr(self, "_settings_backup", True) and os.path.exists(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except Exception:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, path)

    def save_profiles_to_disk(self):
        self._atomic_json_write(PROFILES_FILE, self.profiles)

    def _resolve_sticks(self, p):
        """Zona muerta/curva efectivas de un perfil: fábrica (0/0/1.0)
        salvo que el usuario haya movido los sliders y dado a Guardar
        (sticks_customized). Los valores viejos guardados (8/8) sin esa
        marca se ignoran: el perfil vuelve a fábrica."""
        if p.get("sticks_customized"):
            return (
                p.get("deadzone_l", FACTORY_DEADZONE_L),
                p.get("deadzone_r", FACTORY_DEADZONE_R),
                p.get("curve", FACTORY_CURVE),
            )
        return (FACTORY_DEADZONE_L, FACTORY_DEADZONE_R, FACTORY_CURVE)

    def load_game_profiles(self):
        """Carga el mapeo juego -> perfil: {exe_lower: {name, profile, icon}}."""
        if os.path.exists(GAME_PROFILES_FILE):
            try:
                with open(GAME_PROFILES_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_game_profiles_to_disk(self):
        self._atomic_json_write(GAME_PROFILES_FILE, self.game_profiles)

    def create_tray_image(self):
        # Icono valdomero en el tray si está disponible (antes: dibujo genérico
        # que a tamaño de tray parecía un cassette).
        try:
            ico = _find_valdomero_ico()
            if ico:
                img = Image.open(ico).convert("RGBA")
                return img.resize((64, 64), Image.LANCZOS)
        except Exception:
            pass
        img = Image.new("RGB", (64, 64), color=(18, 22, 33))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([8, 16, 56, 48], radius=14, fill=(59, 130, 246), outline=(147, 197, 253), width=3)
        d.ellipse([16, 26, 26, 36], fill=(255, 255, 255))
        d.ellipse([38, 26, 48, 36], fill=(255, 255, 255))
        return img

    def build_tray_menu(self):
        profile_items = []
        for name in self.profiles.keys():
            profile_items.append(
                pystray.MenuItem(
                    name,
                    self.make_tray_profile_callback(name),
                    checked=lambda item, n=name: self.current_profile_name == n
                )
            )
        return pystray.Menu(
            pystray.MenuItem("Abrir Interfaz", self.show_from_tray, default=True),
            pystray.MenuItem("Perfil Rápido", pystray.Menu(*profile_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", self.quit_app)
        )

    def make_tray_profile_callback(self, name):
        def _cb(icon, item):
            self.after(0, lambda: self.select_profile_external(name))
        return _cb

    def select_profile_external(self, name):
        self.profile_combo.set(name)
        self._on_profile_selected_manual(name)
        if self.tray_icon:
            self.tray_icon.menu = self.build_tray_menu()

    def setup_system_tray(self):
        try:
            self.tray_icon = pystray.Icon(
                "DualSenseStudio", self.create_tray_image(), "DualSense One UI Studio", self.build_tray_menu()
            )
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None  # sin tray no se muere la app

    def hide_to_tray(self):
        self.withdraw()

    def show_from_tray(self, icon=None, item=None):
        self.after(0, self.deiconify)

    def quit_app(self, icon=None, item=None):
        self.running_thread = False
        self.hidcloak.disable()  # restaura tu blacklist/Active previos de HidHide
        self.hw.close()
        if self.tray_icon:
            self.tray_icon.stop()
        self.after(0, self.destroy)

    # ==========================================================================
    # INTERFAZ GRAFICA ESTILO ONE UI (igual que el diseno original)
    # ==========================================================================
    def build_ui(self):
        # Scrollable: con zoom alto la ventana puede ser mas baja que el
        # contenido; el scroll deja llegar abajo sin bajar el zoom del SO.
        main_container = ctk.CTkScrollableFrame(self, fg_color="#121621", corner_radius=0)
        main_container.pack(fill="both", expand=True)
        self._scroll_frame = main_container

        # 1. Barra Superior: DOS filas. En una sola fila los controles de
        # perfil (combo + X + Guardar) quedaban fuera a 960px de ancho y
        # solo aparecian al anchar la ventana.
        top_card = ctk.CTkFrame(main_container, fg_color="#1e2536", corner_radius=18)
        top_card.pack(fill="x", padx=16, pady=(16, 8))
        top_row1 = ctk.CTkFrame(top_card, fg_color="transparent")
        top_row1.pack(fill="x")
        top_row2 = ctk.CTkFrame(top_card, fg_color="transparent")
        top_row2.pack(fill="x", pady=(0, 8))
        # Fila 3: lo que vivía abajo (crear perfil, tray, autostart, log),
        # condensado arriba para que se vea al abrir sin hacer scroll.
        top_row3 = ctk.CTkFrame(top_card, fg_color="transparent")
        top_row3.pack(fill="x", pady=(0, 12))

        self.status_label = ctk.CTkLabel(
            top_row1, text="● Desconectado", font=("Segoe UI", 13, "bold"), text_color="#f87171"
        )
        self.status_label.pack(side="left", padx=16, pady=14)

        self.connect_btn = ctk.CTkButton(
            top_row1, text="Conectar Mando", font=("Segoe UI", 12, "bold"),
            fg_color="#3b82f6", hover_color="#2563eb", corner_radius=10,
            command=self.toggle_service, width=130, height=34
        )
        self.connect_btn.pack(side="left", padx=6)

        # FIX 9: bateria del mando junto al estado
        self.batt_label = ctk.CTkLabel(
            top_row1, text="", font=("Segoe UI", 12), text_color="#93c5fd"
        )
        self.batt_label.pack(side="left", padx=8)

        # FIX 3: switch honesto de HidHide (verifica instalacion real)
        self.hidhide_switch = ctk.CTkSwitch(
            top_row1, text="Anti Doble-Input (HidHide)", font=("Segoe UI", 12),
            command=self.on_hidhide_toggle, progress_color="#3b82f6"
        )
        self.hidhide_switch.pack(side="left", padx=16)

        ctk.CTkLabel(top_row2, text="Perfil:", font=("Segoe UI", 12)).pack(side="left", padx=(8, 4))
        self.profile_combo = ctk.CTkOptionMenu(
            top_row2, values=list(self.profiles.keys()), command=self._on_profile_selected_manual,
            width=180, corner_radius=10, fg_color="#2b3447"
        )
        self.profile_combo.pack(side="left", padx=4)

        # X para eliminar el perfil actual (solo los creados por el usuario)
        self.delete_profile_btn = ctk.CTkButton(
            top_row2, text="✕", width=34, height=34, corner_radius=10,
            fg_color="#7f1d1d", hover_color="#ef4444", font=("Segoe UI", 14, "bold"),
            command=self.delete_current_profile
        )
        self.delete_profile_btn.pack(side="left", padx=2)

        ctk.CTkButton(
            top_row2, text="Guardar", width=85, height=34, corner_radius=10,
            command=lambda: self.save_current_profile(confirm=True), fg_color="#10b981", hover_color="#059669"
        ).pack(side="left", padx=2)

        # Fila 3 (antes: tarjeta de abajo): nuevo perfil + tray + autostart + log.
        ctk.CTkLabel(top_row3, text="Nuevo perfil:", font=("Segoe UI", 12)).pack(side="left", padx=(8, 4))
        self.new_profile_entry = ctk.CTkEntry(
            top_row3, placeholder_text="Nombre del perfil...", width=160, height=34, corner_radius=10, fg_color="#2b3447"
        )
        self.new_profile_entry.pack(side="left", padx=4)

        ctk.CTkButton(
            top_row3, text="Crear", width=80, height=34, corner_radius=10,
            command=self.create_new_profile, fg_color="#3b82f6"
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            top_row3, text="Minimizar a Tray", width=130, height=34, corner_radius=10,
            fg_color="#334155", hover_color="#1e293b", command=self.hide_to_tray
        ).pack(side="left", padx=12)

        # FIX 8: iniciar con Windows
        self.autostart_switch = ctk.CTkSwitch(
            top_row3, text="Iniciar con Windows", font=("Segoe UI", 12),
            command=self.on_autostart_toggle, progress_color="#3b82f6"
        )
        self.autostart_switch.pack(side="left", padx=8)
        if self.get_autostart():
            self.autostart_switch.select()

        # Diagnóstico: log HID
        self.debug_log_switch = ctk.CTkSwitch(
            top_row3, text="Log HID", font=("Segoe UI", 11),
            command=self.on_debug_log_toggle, progress_color="#f59e0b"
        )
        self.debug_log_switch.pack(side="left", padx=8)
        self.debug_log_enabled = False

        # Sección Ajustes (copias de seguridad y restauración)
        ctk.CTkButton(
            top_row3, text="Ajustes", width=80, height=34, corner_radius=10,
            fg_color="#334155", hover_color="#1e293b", font=("Segoe UI", 12),
            command=self._open_settings_dialog
        ).pack(side="left", padx=8)

        # 2. Selector de Emulacion
        emu_card = ctk.CTkFrame(main_container, fg_color="#1e2536", corner_radius=18)
        emu_card.pack(fill="x", padx=16, pady=6)

        ctk.CTkLabel(
            emu_card, text="Emulación Virtual (Game Pass / Steam):", font=("Segoe UI", 13, "bold"), text_color="#93c5fd"
        ).pack(side="left", padx=18, pady=12)

        self.emu_selector = ctk.CTkSegmentedButton(
            emu_card, values=["Xbox 360 (XInput)", "DualShock 4 (PS4)", "Solo Triggers (Sin Emular)"],
            command=self.on_emulation_change, corner_radius=10, fg_color="#2b3447", selected_color="#3b82f6"
        )
        self.emu_selector.pack(side="right", padx=18, pady=12)
        # Por defecto: PS4 (Pedro lo pidió). El usuario puede cambiarlo manual;
        # los perfiles con postura (Anti-cheat / creados) lo cambian al cargarse.
        self.emu_selector.set("DualShock 4 (PS4)")
        self.emulation_type = "DS4"

        # 3. Paneles de Gatillos (L2 y R2) - 4 sliders con visibilidad por modo
        triggers_frame = ctk.CTkFrame(main_container, fg_color="transparent")
        # Sin expand ni peso en la fila: la fila mide lo que mide su contenido.
        # Antes, con expand+weight=1, la fila absorbía todo el alto sobrante
        # de la ventana y el contenedor de sliders (expand) empujaba el
        # botón Restaurar hasta abajo: el hueco enorme al abrir. Al cambiar
        # de perfil el re-layout lo colapsaba; ahora nace ya colapsado.
        triggers_frame.pack(fill="x", padx=16, pady=4)
        triggers_frame.grid_columnconfigure(0, weight=1)
        triggers_frame.grid_columnconfigure(1, weight=1)
        triggers_frame.grid_rowconfigure(0, weight=0)

        self.l2_controls = self.create_trigger_panel(triggers_frame, "Gatillo Izquierdo (L2)", 0, "L2")
        self.r2_controls = self.create_trigger_panel(triggers_frame, "Gatillo Derecho (R2)", 1, "R2")

        # 3b+4. Fila inferior: Juegos (izq, compacto) + Configs (der, 2x2)
        bottom_row = ctk.CTkFrame(main_container, fg_color="transparent")
        bottom_row.pack(fill="x", padx=16, pady=6)
        bottom_row.grid_columnconfigure(0, weight=1)  # juegos ~1/3
        bottom_row.grid_columnconfigure(1, weight=2)  # configs ~2/3
        bottom_row.grid_rowconfigure(0, weight=1)

        # --- IZQUIERDA: Juegos compacto estilo OBS ---
        games_card = ctk.CTkFrame(bottom_row, fg_color="#1e2536", corner_radius=18)
        games_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        games_top = ctk.CTkFrame(games_card, fg_color="transparent")
        games_top.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            games_top, text="🎮 Juegos", font=("Segoe UI", 12, "bold"), text_color="#93c5fd"
        ).pack(side="left")
        self.game_detect_label = ctk.CTkLabel(
            games_top, text="", font=("Segoe UI", 10), text_color="#4ade80"
        )
        self.game_detect_label.pack(side="left", padx=8)

        # Perfil para el juego seleccionado (ancho completo, compacto)
        self.game_profile_menu = ctk.CTkOptionMenu(
            games_card, values=list(self.profiles.keys()), height=28,
            corner_radius=8, fg_color="#2b3447", font=("Segoe UI", 11),
            command=self._on_selected_game_profile_changed
        )
        self.game_profile_menu.pack(fill="x", padx=12, pady=2)

        # Lista compacta con scroll
        self.games_scroll = ctk.CTkScrollableFrame(games_card, height=110, fg_color="#121621", corner_radius=10)
        self.games_scroll.pack(fill="x", padx=12, pady=2)
        self._game_row_widgets = {}
        self._selected_game_exe = None

        # Botones estilo OBS: + | 🗑 | 📝 | Añadir juego actual
        games_btns = ctk.CTkFrame(games_card, fg_color="transparent")
        games_btns.pack(fill="x", padx=12, pady=(2, 10))
        ctk.CTkButton(
            games_btns, text="＋", width=34, height=28, corner_radius=8,
            fg_color="#2b3447", hover_color="#3b82f6", font=("Segoe UI", 14, "bold"),
            command=self.browse_game_exe
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            games_btns, text="🗑", width=34, height=28, corner_radius=8,
            fg_color="#2b3447", hover_color="#ef4444", font=("Segoe UI", 13),
            command=self.remove_selected_game
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            games_btns, text="📝", width=34, height=28, corner_radius=8,
            fg_color="#2b3447", hover_color="#3b82f6", font=("Segoe UI", 13),
            command=self.rename_selected_game
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            games_btns, text="Añadir juego actual", height=28, corner_radius=8,
            fg_color="#3b82f6", hover_color="#2563eb", font=("Segoe UI", 11, "bold"),
            command=self.add_current_game
        ).pack(side="left", padx=6, fill="x", expand=True)
        # Perfiles por flecha: botón a lo ancho, con nombre completo
        # (en la fila de iconos "Flechas" quedaba poco explicativo y no
        # cabía un nombre claro sin apretar los demás botones).
        ctk.CTkButton(
            games_card, text="Perfiles por flecha", height=28, corner_radius=8,
            fg_color="#2b3447", hover_color="#3b82f6", font=("Segoe UI", 11, "bold"),
            command=self._edit_game_quick_switch
        ).pack(fill="x", padx=12, pady=(0, 10))
        self.refresh_game_list_ui()

        # --- DERECHA: Configs en 2x2 ---
        extra_card = ctk.CTkFrame(bottom_row, fg_color="#1e2536", corner_radius=18)
        extra_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        extra_card.grid_columnconfigure((0, 1), weight=1)

        dz_box = ctk.CTkFrame(extra_card, fg_color="transparent")
        dz_box.grid(row=0, column=0, padx=12, pady=8, sticky="nsew")
        ctk.CTkLabel(dz_box, text="Zonas Muertas (Sticks)", font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self.dz_l_lbl = ctk.CTkLabel(dz_box, text="L3: 0%", font=("Segoe UI", 10))
        self.dz_l_lbl.pack(anchor="w", pady=(2, 0))
        self.dz_l_slider = ctk.CTkSlider(
            dz_box, from_=0, to=40, number_of_steps=40, height=12,
            command=lambda v: self._on_dz_slider_moved(self.dz_l_lbl, "L3", v)
        )
        self.dz_l_slider.pack(fill="x", pady=2)

        self.dz_r_lbl = ctk.CTkLabel(dz_box, text="R3: 0%", font=("Segoe UI", 10))
        self.dz_r_lbl.pack(anchor="w", pady=(2, 0))
        self.dz_r_slider = ctk.CTkSlider(
            dz_box, from_=0, to=40, number_of_steps=40, height=12,
            command=lambda v: self._on_dz_slider_moved(self.dz_r_lbl, "R3", v)
        )
        self.dz_r_slider.pack(fill="x", pady=2)

        ctk.CTkButton(
            dz_box, text="Restaurar", width=110, height=28, corner_radius=8,
            command=self._restore_sticks_defaults, fg_color="#475569", hover_color="#64748b",
            font=("Segoe UI", 11)
        ).pack(anchor="w", pady=(6, 2))

        curve_box = ctk.CTkFrame(extra_card, fg_color="transparent")
        curve_box.grid(row=0, column=1, padx=12, pady=8, sticky="nsew")
        ctk.CTkLabel(curve_box, text="Curva de Respuesta", font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self.curve_lbl = ctk.CTkLabel(curve_box, text="Exponente: 1.0 (Lineal)", font=("Segoe UI", 10))
        self.curve_lbl.pack(anchor="w", pady=(6, 2))
        self.curve_slider = ctk.CTkSlider(
            curve_box, from_=0.5, to=2.5, number_of_steps=20, height=12,
            command=lambda v: self._on_curve_slider_moved(v)
        )
        self.curve_slider.pack(fill="x", pady=8)

        touch_box = ctk.CTkFrame(extra_card, fg_color="transparent")
        touch_box.grid(row=1, column=0, padx=12, pady=8, sticky="nsew")
        ctk.CTkLabel(touch_box, text="Touchpad como Cursor", font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self.touch_switch = ctk.CTkSwitch(touch_box, text="Habilitar ratón táctil", font=("Segoe UI", 10),
                                          command=self._on_touch_switch_changed)
        self.touch_switch.pack(anchor="w", pady=(4, 2))

        self.sens_lbl = ctk.CTkLabel(touch_box, text="Velocidad: 1.5x", font=("Segoe UI", 10))
        self.sens_lbl.pack(anchor="w")
        self.sens_slider = ctk.CTkSlider(
            touch_box, from_=0.5, to=4.0, number_of_steps=35, height=12,
            command=lambda v: self._on_sens_slider_moved(v)
        )
        self.sens_slider.pack(fill="x", pady=2)

        # La rueda sobre estos 4 sliders hace scroll del contenedor, no
        # cambia el valor (al hacer scroll, el slider bajo el cursor se
        # movia solo). Solo estos 4; los de gatillos quedan igual.
        for _sl in (self.dz_l_slider, self.dz_r_slider, self.curve_slider, self.sens_slider):
            self._slider_no_wheel(_sl)

        # FIX 9: color de luz desacoplado del perfil
        light_box = ctk.CTkFrame(extra_card, fg_color="transparent")
        light_box.grid(row=1, column=1, padx=12, pady=8, sticky="nsew")
        ctk.CTkLabel(light_box, text="Luz del Mando", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.color_btn = ctk.CTkButton(
            light_box, text="Elegir color", width=110, height=30, corner_radius=8,
            command=self.pick_lightbar_color, font=("Segoe UI", 11)
        )
        self.color_btn.pack(anchor="w", pady=(4, 4))
        self._refresh_color_btn()
        # Presets: por si el diálogo de color falla, estos setean directo
        preset_frame = ctk.CTkFrame(light_box, fg_color="transparent")
        preset_frame.pack(anchor="w")
        for _hex, _rgb in [("#ff0000", (255,0,0)), ("#00ff00", (0,255,0)),
                           ("#0000ff", (0,0,255)), ("#ffff00", (255,255,0)),
                           ("#ff00ff", (255,0,255)), ("#ffffff", (255,255,255))]:
            b = ctk.CTkButton(preset_frame, text="", width=24, height=24, corner_radius=7,
                              fg_color=_hex, hover_color=_hex,
                              command=lambda rgb=_rgb: self.set_lightbar_preset(rgb))
            b.pack(side="left", padx=2)

        # Pie: pista del cambio rápido + sticks de fábrica (pedido por Pedro:
        # que la ventana misma explique lo que no se descubre solo).
        ctk.CTkLabel(
            main_container,
            text="Share + flecha: cambia solo los gatillos (tienes 3 s para la flecha)  •  Sticks en 0% = de fábrica (mueve + Guarda para personalizar)",
            font=("Segoe UI", 10), text_color="#64748b"
        ).pack(fill="x", padx=16, pady=(0, 10))

        # 5. Barra Inferior
    def _on_trigger_slider_moved(self, val_lbl, val):
        """Actualiza el label. No guarda: solo el botón Guardar persiste."""
        val_lbl.configure(text=str(int(val)))
        self._maybe_autosave_profile()

    def _on_dz_slider_moved(self, lbl, prefix, val):
        lbl.configure(text=f"{prefix}: {int(val)}%")
        if not self._loading_profile:
            self._sticks_dirty = True
        self._maybe_autosave_profile()

    def _on_curve_slider_moved(self, val):
        self.curve_lbl.configure(text=f"Exponente: {val:.1f}")
        if not self._loading_profile:
            self._sticks_dirty = True
        self._maybe_autosave_profile()

    def _on_sens_slider_moved(self, val):
        self.sens_lbl.configure(text=f"Velocidad: {val:.1f}x")
        self._maybe_autosave_profile()

    def _slider_no_wheel(self, slider):
        """La rueda del raton sobre este slider hace scroll del contenedor
        en vez de cambiar el valor: reemplaza el binding de rueda del
        slider (y de sus hijos, el canvas interno) por uno que mueve el
        scroll y devuelve 'break'. Solo para los 4 sliders de la zona de
        sticks/curva/touchpad; los de gatillos no se tocan."""
        def _forward(event):
            try:
                frame = getattr(self, "_scroll_frame", None)
                canvas = getattr(frame, "_parent_canvas", None)
                if canvas is None and frame is not None:
                    for _ch in frame.winfo_children():
                        if _ch.winfo_class() == "Canvas":
                            canvas = _ch
                            break
                if canvas is not None:
                    _num = getattr(event, "num", 0)
                    if _num == 4:
                        canvas.yview_scroll(-1, "units")
                    elif _num == 5:
                        canvas.yview_scroll(1, "units")
                    else:
                        canvas.yview_scroll(-1 * int(event.delta / 120), "units")
            except Exception:
                pass
            return "break"
        _seen = set()
        _stack = [slider]
        while _stack:
            _w = _stack.pop()
            if id(_w) in _seen:
                continue
            _seen.add(id(_w))
            for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    _w.bind(_seq, _forward)
                except Exception:
                    pass
            try:
                _stack.extend(_w.winfo_children())
            except Exception:
                pass

    def _restore_sticks_defaults(self):
        """Devuelve los 4 parametros de la zona a FÁBRICA, salvo que el
        perfil tenga sticks personalizados guardados (mover + Guardar):
        descarta cambios sin guardar. La velocidad del touchpad sí
        vuelve a lo guardado. Igual que mover los sliders a mano:
        aplica en vivo, no guarda."""
        p = self.profiles.get(self.current_profile_name) or {}
        base = DEFAULT_PROFILES.get(self.current_profile_name) or {}
        dz_l, dz_r, curve = self._resolve_sticks(p)
        sens = p.get("mouse_sens", base.get("mouse_sens", 1.5))
        self._sticks_dirty = False
        self.dz_l_slider.set(dz_l)
        self.dz_l_lbl.configure(text=f"L3: {int(dz_l)}%")
        self.dz_r_slider.set(dz_r)
        self.dz_r_lbl.configure(text=f"R3: {int(dz_r)}%")
        self.curve_slider.set(curve)
        self.curve_lbl.configure(text=f"Exponente: {curve:.1f}")
        self.sens_slider.set(sens)
        self.sens_lbl.configure(text=f"Velocidad: {sens:.1f}x")
        self._maybe_autosave_profile()

    def create_trigger_panel(self, parent, title, col, side_id):
        panel = ctk.CTkFrame(parent, fg_color="#1e2536", corner_radius=18)
        panel.grid(row=0, column=col, sticky="nsew", padx=6, pady=2)

        ctk.CTkLabel(panel, text=title, font=("Segoe UI", 14, "bold")).pack(pady=(12, 4))

        mode_menu = ctk.CTkOptionMenu(
            panel, values=TRIGGER_MODES_LIST, corner_radius=10, fg_color="#2b3447",
            command=lambda m, s=side_id: self.on_trigger_mode_changed(s, m)
        )
        mode_menu.pack(fill="x", padx=16, pady=(2, 6))

        info_badge = ctk.CTkLabel(
            panel, text="", font=("Segoe UI", 11), text_color="#93c5fd", wraplength=380, justify="left"
        )
        info_badge.pack(fill="x", padx=16, pady=(0, 6))

        sliders_container = ctk.CTkFrame(panel, fg_color="transparent")
        sliders_container.pack(fill="x", padx=8, pady=2)

        sliders = {}
        params = [
            # Validados en hardware por Pedro (modo 0x06):
            ("force", "Frecuencia de Vibración (0 - 255):", 0, 255),  # b[1]
            ("start", "Fuerza de Vibración (0 - 255):", 0, 255),      # b[2]
            ("end", "Posición de Vibración (0 - 255):", 0, 255),      # b[3]
        ]

        for key, label_text, min_v, max_v in params:
            block = ctk.CTkFrame(sliders_container, fg_color="transparent")
            row = ctk.CTkFrame(block, fg_color="transparent")
            row.pack(fill="x", padx=6)
            title_lbl = ctk.CTkLabel(row, text=label_text, font=("Segoe UI", 11))
            title_lbl.pack(side="left")
            val_lbl = ctk.CTkLabel(row, text="0", font=("Consolas", 11, "bold"))
            val_lbl.pack(side="right")

            slider = ctk.CTkSlider(
                block, from_=min_v, to=max_v, number_of_steps=max_v - min_v, height=14,
                command=lambda val, vl=val_lbl: self._on_trigger_slider_moved(vl, val)
            )
            slider.pack(fill="x", padx=6, pady=(1, 4))
            sliders[key] = {
                "block": block,
                "slider": slider,
                "val_lbl": val_lbl,
                "title_lbl": title_lbl
            }

        ctk.CTkButton(
            panel, text="Restaurar gatillo", width=130, height=26, corner_radius=8,
            fg_color="#475569", hover_color="#334155",
            command=lambda s=side_id: self._restore_trigger_side(s)
        ).pack(pady=(4, 10))

        return {"mode": mode_menu, "info": info_badge, "sliders": sliders}

    def _restore_trigger_side(self, side_id):
        """Devuelve UN gatillo (L2 o R2) a lo GUARDADO del perfil actual
        (fábrica si nunca se guardó): modo + 3 sliders. Descarta cambios
        sin guardar de ese lado; aplica en vivo, no guarda."""
        p = self.profiles.get(self.current_profile_name) or {}
        base = DEFAULT_PROFILES.get(self.current_profile_name) or {}
        prefix = side_id.lower()  # "l2" / "r2"
        def _g(k, d):
            return p.get(f"{prefix}_{k}", base.get(f"{prefix}_{k}", d))
        ctrl = self.l2_controls if side_id == "L2" else self.r2_controls
        mode = _g("mode", "Desactivado")
        if mode not in TRIGGER_MODES_LIST:
            mode = "Personalizado (Avanzado)"
        ctrl["mode"].set(mode)
        for k in ["start", "end", "force"]:
            v = int(_g(k, 0))
            ctrl["sliders"][k]["slider"].set(v)
            ctrl["sliders"][k]["val_lbl"].configure(text=str(v))
        self.update_trigger_sliders_visibility(ctrl, mode)
        self.apply_triggers_to_hardware()

    def update_trigger_sliders_visibility(self, controls_dict, mode_name):
        """Los sliders se muestran/ocultan segun el modo (diseno original).
        El slider "force" cambia de etiqueta segun lo que controla en cada modo."""
        sliders = controls_dict["sliders"]
        info = controls_dict["info"]

        for k in sliders:
            sliders[k]["block"].pack_forget()

        if mode_name == "Desactivado":
            info.configure(text="Gatillo libre sin resistencia.")
        elif mode_name == "Disparo de Arma (Clic Fuerte)":
            info.configure(text="Muro rígido al 100% con chasquido seco de clic al superar la pared.")
            sliders["start"]["title_lbl"].configure(text="Punto del Clic (15 - 140):")
            sliders["start"]["block"].pack(fill="x", pady=2)
        elif mode_name == "Disparo Automático (Max Ready)":
            info.configure(text="¡Listo desde la posición 0! Ráfaga apenas tocas el gatillo.")
            sliders["force"]["title_lbl"].configure(text="Velocidad de Ráfaga (0 - 255):")
            sliders["force"]["block"].pack(fill="x", pady=2)
        elif mode_name == "Resistencia Continua":
            info.configure(text="Oposición firme y constante (freno o arco).")
            sliders["start"]["title_lbl"].configure(text="Inicio de Resistencia:")
            sliders["force"]["title_lbl"].configure(text="Fuerza / Dureza:")
            sliders["start"]["block"].pack(fill="x", pady=2)
            sliders["force"]["block"].pack(fill="x", pady=2)
        elif mode_name == "Personalizado (Avanzado)":
            info.configure(text="Vibración: frecuencia, fuerza y posición en el gatillo.")
            sliders["force"]["title_lbl"].configure(text="Frecuencia de Vibración (0 - 255):")
            sliders["start"]["title_lbl"].configure(text="Fuerza de Vibración (0 - 255):")
            sliders["end"]["title_lbl"].configure(text="Posición de Vibración (0 - 255):")
            for k in ["force", "start", "end"]:
                sliders[k]["block"].pack(fill="x", pady=2)

    def on_trigger_mode_changed(self, side_id, new_mode):
        ctrl = self.l2_controls if side_id == "L2" else self.r2_controls
        if new_mode == "Disparo de Arma (Clic Fuerte)":
            ctrl["sliders"]["start"]["slider"].set(35)
            ctrl["sliders"]["start"]["val_lbl"].configure(text="35")
            ctrl["sliders"]["force"]["slider"].set(255)
            ctrl["sliders"]["force"]["val_lbl"].configure(text="255")
        elif new_mode == "Disparo Automático (Max Ready)":
            ctrl["sliders"]["start"]["slider"].set(0)
            ctrl["sliders"]["start"]["val_lbl"].configure(text="0")
            # el slider "force" aqui es velocidad de rafaga; si esta en 0 no vibra
            if int(ctrl["sliders"]["force"]["slider"].get()) == 0:
                ctrl["sliders"]["force"]["slider"].set(255)
                ctrl["sliders"]["force"]["val_lbl"].configure(text="255")

        self.update_trigger_sliders_visibility(ctrl, new_mode)
        self.apply_triggers_to_hardware()
        self._maybe_autosave_profile()

    # FIX 8: autostart con Windows (switch "Iniciar con Windows").
    # - App elevada: tarea programada (logon + highest) -> arranca elevada
    #   SIN prompt UAC. Es lo mas cercano a "guardar" el permiso de admin.
    # - App no elevada: clave Run de HKCU (sin admin). El cloak de HidHide
    #   avisara que necesita admin hasta abrir la app elevada.
    AUTOSTART_NAME = "DualSenseStudio"
    AUTOSTART_TASK = "DualSenseStudio"

    @staticmethod
    def _autostart_command():
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}" --tray'
        pyw = sys.executable.replace('python.exe', 'pythonw.exe')
        script = os.path.abspath(__file__)
        return f'"{pyw}" "{script}" --tray'

    @classmethod
    def _runkey_get(cls):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, cls.AUTOSTART_NAME)
                return True
        except (FileNotFoundError, OSError, ImportError):
            return False

    @classmethod
    def _runkey_set(cls, enable):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, cls.AUTOSTART_NAME, 0,
                                  winreg.REG_SZ, cls._autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, cls.AUTOSTART_NAME)
                except FileNotFoundError:
                    pass

    @classmethod
    def _task_exists(cls):
        try:
            r = subprocess.run(['schtasks', '/query', '/tn', cls.AUTOSTART_TASK],
                               capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    @classmethod
    def _task_set(cls, enable):
        if enable:
            r = subprocess.run(
                ['schtasks', '/create', '/tn', cls.AUTOSTART_TASK,
                 '/tr', cls._autostart_command(),
                 '/sc', 'onlogon', '/rl', 'highest', '/f'],
                capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        r = subprocess.run(['schtasks', '/delete', '/tn', cls.AUTOSTART_TASK, '/f'],
                           capture_output=True, timeout=15)
        return r.returncode == 0

    @classmethod
    def get_autostart(cls):
        return cls._runkey_get() or cls._task_exists()

    @classmethod
    def set_autostart(cls, enable):
        if enable:
            if HidHideController.is_admin():
                if cls._task_set(True):
                    cls._runkey_set(False)  # evita doble arranque
                    return
            cls._runkey_set(True)
        else:
            cls._task_set(False)
            cls._runkey_set(False)

    def on_autostart_toggle(self):
        try:
            self.set_autostart(bool(self.autostart_switch.get()))
        except Exception as e:  # noqa: BLE001
            self.show_alert("Iniciar con Windows", f"No se pudo cambiar:\n{e}")
            # revierte el switch a su estado real
            if self.get_autostart():
                self.autostart_switch.select()
            else:
                self.autostart_switch.deselect()

    def on_debug_log_toggle(self):
        self.debug_log_enabled = bool(self.debug_log_switch.get())
        if self.debug_log_enabled:
            try:
                with open(os.path.join(BASE_DIR, "debug_hid.log"), "a", encoding="utf-8") as f:
                    f.write(f"\n=== Log iniciado {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            except Exception:
                pass

    def _debug_log_write(self, msg):
        if not self.debug_log_enabled:
            return
        try:
            with open(os.path.join(BASE_DIR, "debug_hid.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def on_hidhide_toggle(self):
        # El switch ahora SI configura el cloak (blacklist clasica por IOCTL, con restauracion).
        if not self.hw.connected:
            return  # se aplicara al conectar (start_service)
        if self.hidhide_switch.get():
            self._enable_cloak()
        else:
            self.hidcloak.disable()
        self._maybe_autosave_profile()

    def _enable_cloak(self):
        if not self.hw.hid_path:
            return
        instance_ids = HidHideController.pad_instance_ids(self.hw.hid_path)
        HidHideController._cloak_debug(f"hid_path={self.hw.hid_path} ids={instance_ids}")
        if not instance_ids:
            ok, msg = False, "no se pudo determinar la instancia HID del mando"
        else:
            ok, msg = self.hidcloak.enable(instance_ids)
        if not ok:
            self._debug_log_write(f"CLOAK FAIL: {msg}")
            self.show_alert("Anti Doble-Input",
                            f"No se pudo activar el cloak de HidHide:\n{msg}\n\n"
                            f"Descarga: {HIDHIDE_URL}\n\n"
                            "La app sigue funcionando sin anti doble-input.")

    def show_alert(self, title, message):
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("380x220")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)

        ctk.CTkLabel(dialog, text=title, font=("Segoe UI", 14, "bold"), text_color="#f87171").pack(pady=(16, 4))
        ctk.CTkLabel(dialog, text=message, font=("Segoe UI", 12), justify="center", wraplength=340).pack(pady=4)
        ctk.CTkButton(dialog, text="Entendido", width=100, command=dialog.destroy, fg_color="#3b82f6").pack(pady=(12, 16))

    def _on_profile_selected_manual(self, profile_name):
        """El usuario eligió un perfil manual: cancela el modo auto-juego."""
        self._last_auto_game = None
        self.load_profile_to_ui(profile_name)

    def _scrub_deleted_profile_from_games(self, name):
        """Al eliminar un perfil, los juegos que lo usaban vuelven al ajuste
        de fábrica de ese hueco: perfil base a Gatillos Suaves y cada flecha
        personalizada a su valor general (si el set queda igual al general,
        se suelta y el juego vuelve a 'sin personalizar')."""
        for exe, g in self.game_profiles.items():
            if g.get("profile") == name:
                g["profile"] = "Gatillos Suaves (Estándar)"
            qs = g.get("quick_switch")
            if isinstance(qs, dict):
                for _d in ("up", "down", "left", "right"):
                    if qs.get(_d) == name:
                        qs[_d] = QUICK_SWITCH_MAP[_d]
                if all(qs.get(_d) == QUICK_SWITCH_MAP[_d]
                       for _d in ("up", "down", "left", "right")):
                    g.pop("quick_switch", None)

    def delete_current_profile(self):
        """Elimina el perfil actual si fue creado por el usuario (no los 4 por defecto)."""
        name = self.current_profile_name
        if not name:
            self.show_alert("Ningún perfil seleccionado",
                            "Elige un perfil primero.")
            return
        if name in DEFAULT_PROFILES:
            self.show_alert("No se puede eliminar",
                            f"'{name}' es un perfil predeterminado.\nSolo se pueden eliminar los perfiles que tú creaste.")
            return
        import tkinter.messagebox as msgbox
        if not msgbox.askyesno("Eliminar perfil", f"¿Eliminar el perfil '{name}'?"):
            return
        del self.profiles[name]
        self.save_profiles_to_disk()
        # Quitarlo de juegos que lo usaban (perfil base y flechas)
        self._scrub_deleted_profile_from_games(name)
        self.save_game_profiles_to_disk()
        # Volver al primer perfil
        new_current = list(self.profiles.keys())[0]
        self.profile_combo.configure(values=list(self.profiles.keys()))
        self._on_profile_selected_manual(new_current)
        self.profile_combo.set(new_current)
        self.refresh_game_list_ui()
        if self.tray_icon:
            self.tray_icon.menu = self.build_tray_menu()

    def delete_current_profile(self):
        """Elimina el perfil actual si fue creado por el usuario (no los 4 por defecto)."""
        name = self.current_profile_name
        if not name:
            self.show_alert("Ningún perfil seleccionado",
                            "Elige un perfil primero.")
            return
        if name in DEFAULT_PROFILES:
            self.show_alert("No se puede eliminar",
                            f"'{name}' es un perfil predeterminado.\nSolo se pueden eliminar los perfiles que tú creaste.")
            return
        import tkinter.messagebox as msgbox
        if not msgbox.askyesno("Eliminar perfil", f"¿Eliminar el perfil '{name}'?"):
            return
        del self.profiles[name]
        self.save_profiles_to_disk()
        # Quitarlo de juegos que lo usaban (perfil base y flechas)
        self._scrub_deleted_profile_from_games(name)
        self.save_game_profiles_to_disk()
        # Volver al primer perfil
        new_current = list(self.profiles.keys())[0]
        self.profile_combo.configure(values=list(self.profiles.keys()))
        self._on_profile_selected_manual(new_current)
        self.profile_combo.set(new_current)
        self.refresh_game_list_ui()
        if self.tray_icon:
            self.tray_icon.menu = self.build_tray_menu()

    # ============ JUEGOS: perfil automático ============
    @staticmethod
    def get_foreground_exe():
        """Retorna (exe_lower, exe_full_path) de la ventana al frente, o (None, None)."""
        try:
            import win32gui
            import win32process
            import psutil
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None, None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            exe_path = proc.exe()
            return os.path.basename(exe_path).lower(), exe_path
        except Exception:
            return None, None

    @staticmethod
    def extract_game_icon(exe_path, save_path):
        """Extrae el icono del .exe y lo guarda como PNG. Retorna True si ok."""
        try:
            import win32gui
            import win32ui
            import win32con
            from PIL import Image
            large, _small = win32gui.ExtractIconEx(exe_path, 0)
            if not large:
                return False
            hicon = large[0]
            try:
                hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
                hbmp = win32ui.CreateBitmap()
                hbmp.CreateCompatibleBitmap(hdc, 32, 32)
                hdc_mem = hdc.CreateCompatibleDC()
                hdc_mem.SelectObject(hbmp)
                win32gui.DrawIconEx(hdc_mem.GetHandleOutput(), 0, 0, hicon, 32, 32, 0, None, win32con.DI_NORMAL)
                bmpinfo = hbmp.GetInfo()
                bmpstr = hbmp.GetBitmapBits(True)
                img = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                                       bmpstr, 'raw', 'BGRX', 0, 1)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                img.save(save_path)
                return True
            finally:
                win32gui.DestroyIcon(hicon)
        except Exception:
            return False

    def refresh_game_list_ui(self):
        """Reconstruye la lista compacta de juegos (estilo OBS: filas seleccionables)."""
        if not hasattr(self, 'games_scroll'):
            return
        for w in self.games_scroll.winfo_children():
            w.destroy()
        self._game_row_widgets = {}
        # Sincronizar la lista de perfiles del menú del juego: antes quedaba
        # fija con los valores del arranque y los perfiles creados después
        # no aparecían para asignar a juegos.
        if hasattr(self, 'game_profile_menu'):
            self.game_profile_menu.configure(values=list(self.profiles.keys()))

        if not self.game_profiles:
            ctk.CTkLabel(
                self.games_scroll, text="Sin juegos. Usa ＋ para buscar un .exe o ＋ Juego actual.",
                font=("Segoe UI", 11), text_color="#64748b"
            ).pack(pady=10)
            if hasattr(self, 'game_profile_menu'):
                self.game_profile_menu.set("")
            return

        from PIL import Image as PILImage
        for exe in sorted(self.game_profiles.keys()):
            g = self.game_profiles[exe]
            is_sel = (exe == self._selected_game_exe)
            row = ctk.CTkFrame(
                self.games_scroll,
                fg_color="#3b82f6" if is_sel else "#1e2536",
                corner_radius=8)
            row.pack(fill="x", padx=2, pady=2)

            # Icono pequeño
            icon_path = g.get("icon")
            icon_lbl = None
            if icon_path and os.path.exists(icon_path):
                try:
                    pil_img = PILImage.open(icon_path).resize((22, 22))
                    ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(22, 22))
                    icon_lbl = ctk.CTkLabel(row, text="", image=ctk_img, width=22)
                    icon_lbl.image_ref = ctk_img  # evitar que el GC lo borre
                except Exception:
                    icon_lbl = None
            if icon_lbl is None:
                icon_lbl = ctk.CTkLabel(row, text="🎮", font=("Segoe UI", 14), width=22)
            icon_lbl.pack(side="left", padx=(8, 4), pady=4)

            # Nombre + perfil (texto)
            txt_color = "#ffffff" if is_sel else "#e2e8f0"
            name_lbl = ctk.CTkLabel(row, text=g.get("name", exe), font=("Segoe UI", 11, "bold"),
                                    text_color=txt_color, anchor="w")
            name_lbl.pack(side="left", padx=2)
            prof_lbl = ctk.CTkLabel(row, text=f"→ {g.get('profile', '')}", font=("Segoe UI", 10),
                                    text_color="#93c5fd" if is_sel else "#64748b", anchor="w")
            prof_lbl.pack(side="left", padx=8)

            # Click en la fila = seleccionar (bind en row y labels)
            for widget in (row, icon_lbl, name_lbl, prof_lbl):
                widget.bind("<Button-1>", lambda _e, e=exe: self._select_game_row(e))

            self._game_row_widgets[exe] = {"row": row}

        # Sincronizar el dropdown de perfil con el juego seleccionado
        if hasattr(self, 'game_profile_menu'):
            sel = self.game_profiles.get(self._selected_game_exe, {})
            prof = sel.get("profile", "")
            if prof in self.profiles:
                self.game_profile_menu.set(prof)
            elif self.profiles:
                self.game_profile_menu.set(list(self.profiles.keys())[0])

    def _select_game_row(self, exe):
        self._selected_game_exe = exe
        self.refresh_game_list_ui()

    def _on_selected_game_profile_changed(self, profile_name):
        exe = self._selected_game_exe
        if exe and exe in self.game_profiles and profile_name in self.profiles:
            self.game_profiles[exe]["profile"] = profile_name
            self.save_game_profiles_to_disk()
            self.refresh_game_list_ui()

    def _on_game_profile_changed(self, exe, profile_name):
        # Compat: ya no se usa dropdown por fila, pero se conserva por seguridad
        if exe in self.game_profiles:
            self.game_profiles[exe]["profile"] = profile_name
            self.save_game_profiles_to_disk()

    def remove_selected_game(self):
        """Botón 🗑 estilo OBS: elimina el juego seleccionado."""
        exe = self._selected_game_exe
        if not exe or exe not in self.game_profiles:
            self.show_alert("Nada seleccionado", "Toca un juego de la lista primero.")
            return
        import tkinter.messagebox as msgbox
        g = self.game_profiles.get(exe, {})
        if msgbox.askyesno("Quitar juego", f"¿Quitar '{g.get('name', exe)}' de la lista?"):
            self.game_profiles.pop(exe, None)
            self.save_game_profiles_to_disk()
            if self._last_auto_game == exe:
                self._last_auto_game = None
            if self._selected_game_exe == exe:
                self._selected_game_exe = None
            self.refresh_game_list_ui()

    def rename_selected_game(self):
        """Botón 📝 estilo OBS: cambia solo el nombre visible del juego.
        El exe (la clave del mapeo) NO se toca: la detección y el perfil
        siguen funcionando exactamente igual; solo cambia la etiqueta que
        se muestra en la lista y en el popup."""
        exe = self._selected_game_exe
        if not exe or exe not in self.game_profiles:
            self.show_alert("Nada seleccionado", "Toca un juego de la lista primero.")
            return
        g = self.game_profiles[exe]
        old_name = g.get("name", exe)
        dialog = ctk.CTkToplevel(self)
        dialog.title("Renombrar juego")
        dialog.geometry("380x190")
        dialog.transient(self)
        dialog.attributes("-topmost", True)
        # Sin grab: así los avisos (show_alert) siguen siendo clicables.
        ctk.CTkLabel(dialog, text=f"Exe: {exe}", font=("Segoe UI", 10),
                     text_color="#64748b").pack(pady=(16, 4))
        ctk.CTkLabel(dialog, text="Nuevo nombre:", font=("Segoe UI", 11)).pack(anchor="w", padx=20)
        name_entry = ctk.CTkEntry(dialog, width=340, height=32, corner_radius=8)
        name_entry.insert(0, old_name)
        name_entry.pack(padx=20, pady=(2, 8))
        try:
            name_entry.focus_set()
        except Exception:
            pass
        try:
            name_entry.select_range(0, "end")
        except Exception:
            pass

        def _save():
            new_name = name_entry.get().strip()
            if not new_name:
                self.show_alert("Nombre vacío", "Escribe un nombre o cancela.")
                return
            if new_name != old_name:
                self.game_profiles[exe]["name"] = new_name
                self.save_game_profiles_to_disk()
                self.refresh_game_list_ui()
                try:
                    if self._last_auto_game == exe:
                        self.game_detect_label.configure(text=f"▶ {new_name}", text_color="#4ade80")
                except Exception:
                    pass
            dialog.destroy()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=8)
        ctk.CTkButton(btn_row, text="Guardar", width=120, fg_color="#10b981",
                      command=_save).pack(side="left", padx=8)
        ctk.CTkButton(btn_row, text="Cancelar", width=120, fg_color="#475569",
                      command=dialog.destroy).pack(side="left", padx=8)

    # ======================================================================
    # ACTUALIZACIONES OTA (GitHub Releases): aviso + botón, el usuario
    # decide. Nunca automático, nunca en partida: el aviso solo sale con
    # 5s seguidos sin ningún juego mapeado corriendo. "Después" pospone
    # 24h ESA versión. Mientras el repo sea privado, el chequeo necesita
    # github_token.txt (token de solo lectura para este repo) junto al
    # .exe; al hacer el repo público, ese archivo puede borrarse.
    # ======================================================================
    def _ota_token(self):
        try:
            with open(os.path.join(BASE_DIR, "github_token.txt"), "r", encoding="utf-8") as f:
                tok = f.read().strip()
            return tok or None
        except Exception:
            return None

    def _ota_check_in_background(self, manual=False):
        import threading
        if getattr(self, "_ota_checking", False):
            return
        self._ota_checking = True

        def _work():
            info = None
            try:
                info = self._ota_fetch_latest()
            except Exception:
                info = None
            finally:
                self._ota_checking = False
            try:
                self.after(0, lambda: self._ota_on_check_result(info, manual))
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True).start()

    def _ota_fetch_latest(self):
        import urllib.request
        req = urllib.request.Request(OTA_RELEASES_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "DualSenseStudio",
        })
        tok = self._ota_token()
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        tag = str(data.get("tag_name") or "")
        if not tag:
            return None
        asset_url = None
        sha_url = None
        for a in data.get("assets") or []:
            if a.get("name") == OTA_ASSET_NAME:
                asset_url = a.get("url")
            elif a.get("name") == OTA_ASSET_NAME + ".sha256":
                sha_url = a.get("url")
        if not asset_url:
            return None
        return {"tag": tag, "asset_url": asset_url, "sha_url": sha_url}

    def _ota_on_check_result(self, info, manual=False):
        try:
            if info is not None:
                tag = info.get("tag")
                if _ota_is_newer(tag, OTA_VERSION):
                    if getattr(self, "_ota_available", None) is None or getattr(self, "_ota_available").get("tag") != tag:
                        self._ota_prompted_key = None
                    self._ota_available = info
                else:
                    self._ota_available = None
            self._ota_refresh_settings_ui()
            if manual:
                if getattr(self, "_ota_available", None):
                    self._ota_show_prompt(self._ota_available)
                elif info is not None:
                    self.show_alert("Estás al día", f"Ya tienes la última versión ({OTA_VERSION}).")
                else:
                    self.show_alert("No se pudo buscar", "No se pudo contactar GitHub. Revisa tu internet (si el repo es privado, falta github_token.txt junto al .exe).")
        except Exception:
            pass

    def _ota_refresh_settings_ui(self):
        try:
            lbl = getattr(self, "_ota_settings_status", None)
            if lbl is not None and lbl.winfo_exists():
                info = getattr(self, "_ota_available", None)
                if info:
                    lbl.configure(text=f"Versión instalada: {OTA_VERSION}  •  Disponible: {info.get('tag')}")
                else:
                    lbl.configure(text=f"Versión instalada: {OTA_VERSION}  •  Al día")
            btn = getattr(self, "_ota_install_btn", None)
            if btn is not None and btn.winfo_exists():
                btn.configure(state="normal" if getattr(self, "_ota_available", None) else "disabled")
        except Exception:
            pass

    def _ota_prompt_if_idle(self):
        """Aviso de actualización solo en idle: 5s seguidos sin ningún
        juego mapeado corriendo. Nunca sale sobre una partida."""
        try:
            info = getattr(self, "_ota_available", None)
            if not info:
                return
            tag = info.get("tag")
            if not tag or not _ota_is_newer(tag, OTA_VERSION):
                return
            if _ota_snooze_active(getattr(self, "_update_snooze_until", 0),
                                  getattr(self, "_update_snooze_version", ""),
                                  tag, time.time()):
                return
            old = getattr(self, "_ota_prompt_win", None)
            if old is not None:
                try:
                    if old.winfo_exists():
                        return
                except Exception:
                    pass
                self._ota_prompt_win = None
            names = getattr(self, "_last_seen_process_names", None)
            if names is None:
                return
            now = time.time()
            if any(exe in names for exe in self.game_profiles):
                self._update_idle_since = None
                return
            if getattr(self, "_update_idle_since", None) is None:
                self._update_idle_since = now
                return
            if now - self._update_idle_since < OTA_IDLE_SECONDS:
                return
            key = (self._update_idle_since, tag)
            if getattr(self, "_ota_prompted_key", None) == key:
                return
            self._ota_prompted_key = key
            self.after(0, lambda: self._ota_show_prompt(info))
        except Exception:
            pass

    def _ota_show_prompt(self, info):
        try:
            old = getattr(self, "_ota_prompt_win", None)
            if old is not None:
                try:
                    if old.winfo_exists():
                        return
                except Exception:
                    pass
            tag = info.get("tag")
            win = ctk.CTkToplevel(self)
            win.title("Actualización disponible")
            win.geometry("380x190")
            win.resizable(False, False)
            win.transient(self)
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="Actualización disponible", font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))
            ctk.CTkLabel(win, text=f"Hay una versión nueva ({tag}).\n¿Quieres instalarla ahora?",
                         font=("Segoe UI", 12), justify="center").pack(pady=4)
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(pady=(10, 14))
            ctk.CTkButton(row, text="Actualizar ahora", width=150, height=34, corner_radius=8, fg_color="#16a34a",
                          command=lambda: (self._ota_close_prompt(), self._ota_start_install(info))).pack(side="left", padx=6)
            ctk.CTkButton(row, text="Después", width=120, height=34, corner_radius=8, fg_color="#475569",
                          command=lambda: self._ota_snooze(tag)).pack(side="left", padx=6)
            win.protocol("WM_DELETE_WINDOW", lambda: self._ota_snooze(tag))
            self._ota_prompt_win = win
        except Exception:
            pass

    def _ota_close_prompt(self):
        try:
            win = getattr(self, "_ota_prompt_win", None)
            self._ota_prompt_win = None
            if win is not None:
                win.destroy()
        except Exception:
            pass

    def _ota_snooze(self, tag):
        try:
            self._update_snooze_until = time.time() + OTA_SNOOZE_SECONDS
            self._update_snooze_version = str(tag)
            data = {}
            try:
                if os.path.exists(SETTINGS_FILE):
                    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
            except Exception:
                data = {}
            data["update_snooze_until"] = self._update_snooze_until
            data["update_snooze_version"] = self._update_snooze_version
            self._atomic_json_write(SETTINGS_FILE, data)
        except Exception:
            pass
        self._ota_close_prompt()

    def _ota_install_from_settings(self, dialog):
        info = getattr(self, "_ota_available", None)
        if not info:
            self.show_alert("Sin actualización", "No hay ninguna actualización disponible.")
            return
        try:
            dialog.destroy()
        except Exception:
            pass
        self._ota_start_install(info)

    def _ota_start_install(self, info):
        import threading
        if not getattr(sys, "frozen", False):
            self.show_alert("Solo .exe", "La actualización automática solo funciona con el .exe compilado, no desde el .py.")
            return
        self._ota_close_prompt()
        self.show_alert("Descargando", f"Descargando la versión {info.get('tag')}... La app se cerrará sola al terminar.")

        def _work():
            try:
                self._ota_download_and_install(info)
            except Exception as e:
                try:
                    self.after(0, lambda: self.show_alert("No se pudo actualizar", f"Error al descargar:\n{e}"))
                except Exception:
                    pass
        threading.Thread(target=_work, daemon=True).start()

    def _ota_download_and_install(self, info):
        import urllib.request
        import hashlib
        import subprocess
        exe_path = os.path.abspath(sys.executable)
        new_path = exe_path + ".new"
        tok = self._ota_token()
        req = urllib.request.Request(info["asset_url"], headers={
            "Accept": "application/octet-stream",
            "User-Agent": "DualSenseStudio",
        })
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        h = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(req, timeout=60) as resp, open(new_path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
                out.write(chunk)
        if total < 1000000:
            raise RuntimeError("Descarga incompleta (archivo demasiado chico).")
        sha_url = info.get("sha_url")
        if sha_url:
            req2 = urllib.request.Request(sha_url, headers={
                "Accept": "application/octet-stream",
                "User-Agent": "DualSenseStudio",
            })
            if tok:
                req2.add_header("Authorization", "Bearer " + tok)
            with urllib.request.urlopen(req2, timeout=15) as resp:
                expected = resp.read().decode("utf-8", "ignore").split()[0].strip().lower()
            if expected and h.hexdigest() != expected:
                raise RuntimeError("La descarga no pasó la verificación (hash distinto).")
        bat_path = os.path.join(BASE_DIR, "actualizar_dualsense.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(_ota_updater_script(exe_path, new_path, os.path.basename(exe_path)))
        try:
            subprocess.Popen(["cmd", "/c", bat_path],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except Exception:
            subprocess.Popen(["cmd", "/c", bat_path])
        self.after(0, lambda: self.quit_app())

    def _open_settings_dialog(self):
        """Sección Ajustes: copias de seguridad y restauración. Mismo
        lenguaje visual que el diálogo de renombrar (sin grab, topmost)."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Ajustes")
        dialog.geometry("400x380")
        dialog.transient(self)
        dialog.attributes("-topmost", True)
        # Sin grab: así los avisos (show_alert) siguen siendo clicables.
        ctk.CTkLabel(dialog, text="Ajustes", font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))
        ctk.CTkLabel(dialog, text="Cada guardado escribe a un archivo temporal y lo renombra (un corte no corrompe nada) y, si las copias están activadas, guarda antes una copia .bak del archivo anterior.",
                     font=("Segoe UI", 11), text_color="#64748b", wraplength=340).pack(padx=20, pady=(0, 10))

        backup_switch = ctk.CTkSwitch(
            dialog, text="Copias de seguridad (.bak)", font=("Segoe UI", 12),
            command=lambda: self._on_backup_toggle(backup_switch),
            progress_color="#3b82f6")
        backup_switch.pack(anchor="w", padx=20, pady=4)
        if getattr(self, "_settings_backup", True):
            backup_switch.select()

        ctk.CTkButton(dialog, text="Restaurar copias de seguridad", width=240, height=32,
                      corner_radius=8, fg_color="#475569",
                      command=lambda: self._restore_backups(dialog)).pack(pady=10)

        ctk.CTkLabel(dialog, text="Actualizaciones", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=20, pady=(6, 0))
        self._ota_settings_status = ctk.CTkLabel(dialog, text=f"Versión instalada: {OTA_VERSION}",
                                                 font=("Segoe UI", 11), text_color="#64748b", wraplength=340)
        self._ota_settings_status.pack(anchor="w", padx=20)
        _ota_btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        _ota_btn_row.pack(pady=6)
        ctk.CTkButton(_ota_btn_row, text="Buscar actualizaciones", width=170, height=32, corner_radius=8,
                      fg_color="#475569", command=lambda: self._ota_check_in_background(True)).pack(side="left", padx=(0, 6))
        self._ota_install_btn = ctk.CTkButton(_ota_btn_row, text="Instalar actualización", width=170, height=32,
                                              corner_radius=8, fg_color="#16a34a",
                                              command=lambda: self._ota_install_from_settings(dialog))
        self._ota_install_btn.pack(side="left")
        self._ota_refresh_settings_ui()

        ctk.CTkButton(dialog, text="Cerrar", width=130, height=32, corner_radius=8,
                      fg_color="#3b82f6", command=dialog.destroy).pack(pady=(0, 14))

    def _on_backup_toggle(self, switch):
        try:
            self._settings_backup = bool(switch.get())
        except Exception:
            pass
        try:
            self.save_settings_to_disk()
        except Exception:
            pass

    def _restore_backups(self, dialog=None):
        import tkinter.messagebox as msgbox
        import shutil
        found = [p for p in (PROFILES_FILE, GAME_PROFILES_FILE) if os.path.exists(p + ".bak")]
        if not found:
            self.show_alert("Sin copias", "No hay copias .bak para restaurar.")
            return
        names = ", ".join(os.path.basename(p) for p in found)
        if not msgbox.askyesno("Restaurar copias",
                               f"¿Restaurar desde las copias .bak ({names})?\nSe perderán los cambios hechos después de la última copia."):
            return
        try:
            for path in found:
                shutil.copyfile(path + ".bak", path)
            self.profiles = self.load_profiles()
            self.game_profiles = self.load_game_profiles()
            self.profile_combo.configure(values=list(self.profiles.keys()))
            if self.current_profile_name in self.profiles:
                self.load_profile_to_ui(self.current_profile_name)
                self.profile_combo.set(self.current_profile_name)
            self.refresh_game_list_ui()
            self.show_alert("Copias restauradas", f"Restaurado desde: {names}.")
        except Exception as e:
            self.show_alert("No se pudo restaurar", f"Error al restaurar:\n{e}")
            return
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:
                pass

    def _edit_game_quick_switch(self):
        """Editor del set de flechas del juego seleccionado: qué perfil abre
        cada flecha en ESE juego. Sin personalizar, usa el ajuste general.
        Guarda al instante, como el menú de perfil del juego (mismo lenguaje
        visual que el diálogo de renombrar: sin grab, topmost, botones
        verde/gris/azul)."""
        exe = self._selected_game_exe
        if not exe or exe not in self.game_profiles:
            self.show_alert("Nada seleccionado", "Toca un juego de la lista primero.")
            return
        g = self.game_profiles[exe]
        game_name = g.get("name", exe)
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Cambio rápido — {game_name}")
        dialog.geometry("420x330")
        dialog.transient(self)
        dialog.attributes("-topmost", True)
        # Sin grab: así los avisos (show_alert) siguen siendo clicables.
        ctk.CTkLabel(
            dialog, text=f"Qué perfil abre cada flecha en {game_name}.",
            font=("Segoe UI", 12, "bold")
        ).pack(pady=(16, 2))
        ctk.CTkLabel(
            dialog, text="Sin tocar nada, usa el ajuste general.",
            font=("Segoe UI", 11), text_color="#64748b"
        ).pack(pady=(0, 10))

        effective = self._resolve_quick_switch_map(exe)
        menus = {}
        rows = (("up", "↑", "Arriba"), ("down", "↓", "Abajo"),
                ("left", "←", "Izquierda"), ("right", "→", "Derecha"))
        status_lbl = ctk.CTkLabel(dialog, text="", font=("Segoe UI", 11))

        def _refresh_status():
            custom = bool(g.get("quick_switch"))
            status_lbl.configure(
                text="Personalizado para este juego" if custom else "Usando el ajuste general",
                text_color="#4ade80" if custom else "#64748b")

        def _on_arrow_changed(_value, direction):
            cur = {d: menus[d].get() for d in ("up", "down", "left", "right")}
            if all(cur[d] == QUICK_SWITCH_MAP[d] for d in cur):
                g.pop("quick_switch", None)
            else:
                g["quick_switch"] = dict(cur)
            self.save_game_profiles_to_disk()
            _refresh_status()

        for direction, glyph, label in rows:
            row = ctk.CTkFrame(dialog, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(row, text=glyph, font=("Segoe UI", 14, "bold"),
                         fg_color="#3b82f6", text_color="#ffffff",
                         width=30, height=30, corner_radius=8).pack(side="left")
            ctk.CTkLabel(row, text=label, font=("Segoe UI", 11),
                         width=70, anchor="w").pack(side="left", padx=(8, 4))
            menu = ctk.CTkOptionMenu(
                row, values=list(self.profiles.keys()), height=30,
                corner_radius=8, fg_color="#2b3447", font=("Segoe UI", 11),
                command=lambda v, d=direction: _on_arrow_changed(v, d)
            )
            menu.pack(side="left", fill="x", expand=True)
            menu.set(effective.get(direction, QUICK_SWITCH_MAP[direction]))
            menus[direction] = menu

        def _reset():
            g.pop("quick_switch", None)
            self.save_game_profiles_to_disk()
            for d, m in menus.items():
                m.set(QUICK_SWITCH_MAP[d])
            _refresh_status()

        status_lbl.pack(pady=(8, 2))
        _refresh_status()
        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=(4, 14))
        ctk.CTkButton(btn_row, text="Restablecer", width=130, fg_color="#475569",
                      command=_reset).pack(side="left", padx=8)
        ctk.CTkButton(btn_row, text="Cerrar", width=130, fg_color="#3b82f6",
                      command=dialog.destroy).pack(side="left", padx=8)

    def remove_game(self, exe):
        # Compat con llamadas viejas
        self._selected_game_exe = exe
        self.remove_selected_game()

    def browse_game_exe(self):
        """Botón ＋ estilo OBS: buscar el .exe del juego manualmente (o acceso directo de Steam .url)."""
        from tkinter import filedialog
        exe_path = filedialog.askopenfilename(
            title="Selecciona el .exe del juego (o acceso directo de Steam)",
            filetypes=[("Ejecutables y accesos Steam", "*.exe *.url"), ("Ejecutables", "*.exe"),
                       ("Accesos Steam", "*.url"), ("Todos", "*.*")])
        if not exe_path:
            return
        # Si es un acceso directo de Steam (.url), resolver al .exe real
        if exe_path.lower().endswith(".url"):
            real_exe = self.resolve_steam_shortcut(exe_path)
            if not real_exe:
                return  # resolve_steam_shortcut ya mostró el mensaje
            exe_path = real_exe
        exe_lower = os.path.basename(exe_path).lower()
        if exe_lower in self.game_profiles:
            self.show_alert("Ya está", f"'{exe_lower}' ya está en la lista.")
            return
        suggested = os.path.splitext(exe_lower)[0].replace("_", " ").replace("-", " ").title()
        icon_path = os.path.join(GAME_ICONS_DIR, f"{exe_lower}.png")
        if not self.extract_game_icon(exe_path, icon_path):
            icon_path = ""
        self.game_profiles[exe_lower] = {
            "name": suggested,
            "profile": self.current_profile_name,
            "icon": icon_path,
            "exe_path": exe_path,
        }
        self.save_game_profiles_to_disk()
        self._selected_game_exe = exe_lower
        self.refresh_game_list_ui()

    @staticmethod
    def resolve_steam_shortcut(url_path):
        """Dado un .url de Steam (steam://rungameid/XXX), encuentra el .exe real del juego.
        Retorna la ruta del exe o None (con mensaje al usuario)."""
        try:
            with open(url_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            return None
        m = re.search(r"steam://rungameid/(\d+)", content)
        if not m:
            # No es un acceso de Steam, avisar
            from tkinter import messagebox as msgbox
            msgbox.showinfo("No es de Steam",
                            "Ese acceso directo no es de Steam.\nSelecciona el .exe directamente o usa 'Añadir juego actual' con el juego abierto.")
            return None
        appid = m.group(1)
        exe_candidates = DualSenseStudioApp.find_steam_game_exes(appid)
        if not exe_candidates:
            from tkinter import messagebox as msgbox
            msgbox.showinfo("No encontrado",
                            f"No encontré el juego de Steam (App ID {appid}) instalado.\n"
                            "Abre el juego y usa 'Añadir juego actual' para capturarlo.")
            return None
        if len(exe_candidates) == 1:
            return exe_candidates[0]
        # Varios exes: dejar que el usuario elija
        return DualSenseStudioApp.pick_exe_from_list(exe_candidates)

    @staticmethod
    def find_steam_game_exes(appid):
        """Busca los .exe del juego de Steam con ese App ID. Retorna lista de rutas."""
        steam_roots = []
        # 1. Registro de Windows
        try:
            import winreg
            for hive, sub in [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                              (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")]:
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        p, _ = winreg.QueryValueEx(k, "InstallPath")
                        if p and os.path.isdir(p):
                            steam_roots.append(p)
                except Exception:
                    pass
        except Exception:
            pass
        # 2. Rutas comunes
        for p in [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]:
            if os.path.isdir(p) and p not in steam_roots:
                steam_roots.append(p)
        if not steam_roots:
            return []

        libraries = []
        for root in steam_roots:
            libraries.append(os.path.join(root, "steamapps"))
            # libraryfolders.vdf puede tener más bibliotecas
            vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
            if os.path.exists(vdf):
                try:
                    with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            m = re.search(r'"path"\s+"([^"]+)"', line)
                            if m:
                                lib = os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps")
                                if os.path.isdir(lib) and lib not in libraries:
                                    libraries.append(lib)
                except Exception:
                    pass

        for lib in libraries:
            manifest = os.path.join(lib, f"appmanifest_{appid}.acf")
            if not os.path.exists(manifest):
                continue
            installdir = None
            try:
                with open(manifest, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = re.search(r'"installdir"\s+"([^"]+)"', line)
                        if m:
                            installdir = m.group(1)
                            break
            except Exception:
                pass
            if not installdir:
                continue
            game_dir = os.path.join(lib, "common", installdir)
            if not os.path.isdir(game_dir):
                continue
            # Buscar exes (no muy profundo para no tardar)
            exes = []
            for dirpath, _dirnames, filenames in os.walk(game_dir):
                # No bajar más de 3 niveles desde game_dir
                rel = os.path.relpath(dirpath, game_dir)
                if rel.count(os.sep) > 3:
                    continue
                for fn in filenames:
                    if fn.lower().endswith(".exe"):
                        # Ignorar uninstallers y redist
                        low = fn.lower()
                        if any(k in low for k in ["uninstall", "redist", "vcredist", "dxsetup", "dotne"]):
                            continue
                        exes.append(os.path.join(dirpath, fn))
                if len(exes) > 20:
                    break
            return sorted(exes)
        return []

    @staticmethod
    def pick_exe_from_list(exes):
        """Diálogo simple para elegir entre varios .exe encontrados."""
        dialog = ctk.CTkToplevel()
        dialog.title("Elige el ejecutable del juego")
        dialog.geometry("520x320")
        dialog.transient()
        dialog.grab_set()
        ctk.CTkLabel(dialog, text="Se encontraron varios .exe. Elige el del juego:",
                     font=("Segoe UI", 12, "bold"), wraplength=480).pack(pady=(14, 8))
        scroll = ctk.CTkScrollableFrame(dialog, height=180)
        scroll.pack(fill="x", padx=16, pady=4)
        chosen = {"path": None}

        def _pick(p):
            chosen["path"] = p
            dialog.destroy()

        for p in exes:
            ctk.CTkButton(
                scroll, text=os.path.basename(p), anchor="w",
                fg_color="#2b3447", hover_color="#3b82f6",
                command=lambda pp=p: _pick(pp)
            ).pack(fill="x", pady=2)
        ctk.CTkButton(dialog, text="Cancelar", width=120, fg_color="#475569",
                      command=dialog.destroy).pack(pady=10)
        dialog.wait_window()
        return chosen["path"]

    def add_current_game(self):
        """Captura el juego de la ventana al frente (con cuenta regresiva de 20s
        para que el usuario ponga el juego al frente)."""
        # Diálogo con cuenta regresiva: el usuario necesita tiempo para poner el juego al frente
        dialog = ctk.CTkToplevel(self)
        dialog.title("Capturar juego actual")
        dialog.geometry("380x200")
        dialog.transient(self)
        # Sin grab: la cuenta no necesita bloquear la ventana principal.

        ctk.CTkLabel(dialog, text="Pon el JUEGO al frente.",
                     font=("Segoe UI", 13, "bold"), text_color="#93c5fd",
                     wraplength=340).pack(pady=(16, 4))
        ctk.CTkLabel(dialog, text="Tienes 20 segundos. Haz clic en el juego para que sea la ventana activa.",
                     font=("Segoe UI", 11), text_color="#64748b",
                     wraplength=340).pack(pady=(0, 8))
        count_lbl = ctk.CTkLabel(dialog, text="20", font=("Segoe UI", 28, "bold"),
                                 text_color="#4ade80")
        count_lbl.pack(pady=8)

        cancelled = {"flag": False}

        def _cancel():
            cancelled["flag"] = True
            dialog.destroy()

        ctk.CTkButton(dialog, text="Cancelar", width=120, fg_color="#475569",
                      command=_cancel).pack(pady=8)

        def _tick(n):
            if cancelled["flag"]:
                return
            if n <= 0:
                dialog.destroy()
                self.after(100, self._capture_foreground_game)
                return
            count_lbl.configure(text=str(n))
            dialog.after(1000, lambda: _tick(n - 1))

        _tick(20)

    def _capture_foreground_game(self):
        """Captura el exe de la ventana al frente (llamado tras la cuenta regresiva)."""
        exe_lower, exe_path = self.get_foreground_exe()
        if not exe_lower:
            self.show_alert("No se detectó", "No pude detectar la ventana al frente.\nAbre el juego y ponlo al frente.")
            return
        # Ignorar la propia app y explorador
        if exe_lower in ("dualsensestudio.exe", "explorer.exe", "python.exe", "pythonw.exe"):
            self.show_alert("Ventana no válida",
                            f"La ventana al frente es '{exe_lower}'.\nPon el JUEGO al frente (no el launcher ni esta app).")
            return
        if exe_lower in self.game_profiles:
            self.show_alert("Ya está", f"'{exe_lower}' ya está en la lista.")
            return

        # Ventana del juego AHORA (está al frente tras la cuenta): guardarla
        # para devolverla al frente al Agregar/Cancelar, sin depender de
        # adivinar su exe (el capturado puede ser un launcher/probe).
        try:
            game_hwnd = win32gui.GetForegroundWindow()
        except Exception:
            game_hwnd = None

        # Diálogo: nombre + perfil (alto según nº de perfiles: radios, no
        # desplegable — el desplegable era otra ventana que robaba el foco).
        dialog = ctk.CTkToplevel(self)
        dialog.title("Agregar juego")
        dialog.geometry(f"380x{320 + 32 * len(self.profiles)}")
        dialog.transient(self)
        # Sin grab: no hace falta bloquear la ventana principal, y el modal
        # interfiere con el no-foco del cuadro.
        # Al frente del juego: sin esto el diálogo quedaba detrás del juego
        # (que está al frente tras la cuenta) y había que buscarlo con Alt+Tab.
        dialog.attributes("-topmost", True)
        try:
            dialog.lift()
        except Exception:
            pass
        # Igual que el popup: NO robar el foco, o Windows minimiza el juego.
        # El cuadro se dibuja encima y recibe clics sin quitarle el primer
        # plano al juego; al Agregar/Cancelar solo se cierra y el juego queda
        # al frente. Sin foco no entran teclas al nombre: para cambiarlo,
        # clic en el campo (toma el foco a propósito, ver más abajo).
        try:
            hwnd = dialog.winfo_id()
            exstyle = win32gui.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            win32gui.SetWindowLongW(hwnd, -20, exstyle | 0x08000000)  # WS_EX_NOACTIVATE
        except Exception:
            pass
        # Además: subclass para que el clic no active la ventana (el wndproc
        # de Tk ignora el NOACTIVATE y por eso el clic minimizaba el juego).
        # Se guarda si instaló bien para mostrarlo en el cuadro (diagnóstico).
        subclass_ok = self._subclass_noactivate(dialog)

        ctk.CTkLabel(dialog, text=f"Detectado: {exe_lower}", font=("Segoe UI", 12, "bold"),
                     text_color="#93c5fd", wraplength=340).pack(pady=(16, 8))
        ctk.CTkLabel(dialog, text="Si capturó el launcher en vez del juego, ciérralo y reintenta con el juego al frente.",
                     font=("Segoe UI", 10), text_color="#64748b", wraplength=340).pack(pady=(0, 8))
        ctk.CTkLabel(dialog, text=("Anti-foco (subclass): OK" if subclass_ok else "Anti-foco (subclass): FALLÓ — avísame"),
                     font=("Segoe UI", 9), text_color="#64748b").pack(pady=(0, 4))

        ctk.CTkLabel(dialog, text="Nombre del juego:", font=("Segoe UI", 11)).pack(anchor="w", padx=20)
        name_entry = ctk.CTkEntry(dialog, width=340, height=32, corner_radius=8)
        # Sugerir nombre sin extensión y capitalizado
        suggested = os.path.splitext(exe_lower)[0].replace("_", " ").replace("-", " ").title()
        name_entry.insert(0, suggested)
        name_entry.pack(padx=20, pady=(2, 8))

        def _focus_name_entry(event=None):
            # Sin foco no entran teclas al cuadro: si el usuario quiere
            # cambiar el nombre, el clic en el campo toma el foco a propósito
            # (el juego puede minimizarse en ese caso, pero al Agregar se
            # restaura: ver _restore_game_window).
            try:
                hwnd = dialog.winfo_id()
                exstyle = win32gui.GetWindowLongW(hwnd, -20)
                win32gui.SetWindowLongW(hwnd, -20, exstyle & ~0x08000000)
            except Exception:
                pass
            try:
                dialog.focus_force()
            except Exception:
                pass
            try:
                name_entry.focus_set()
            except Exception:
                pass
        name_entry.bind("<Button-1>", _focus_name_entry)

        ctk.CTkLabel(dialog, text="Perfil a activar:", font=("Segoe UI", 11)).pack(anchor="w", padx=20)
        prof_var = ctk.StringVar(value=self.current_profile_name or "")
        # Radios DENTRO del cuadro, no desplegable: el desplegable abría otra
        # ventana que al hacer clic robaba el foco y minimizaba el juego.
        def _bind_nofocus(widget, on_press):
            # Interceptar el clic ANTES que el manejador de CTk y romper la
            # cadena: el manejador propio de CTk pone el foco en el widget
            # (SetFocus programático, que NOACTIVATE no bloquea) y eso le
            # quita el primer plano al juego y lo minimiza. Aquí el clic
            # solo hace su acción, sin tocar el foco jamás.
            widget.bind("<Button-1>", on_press)
            widget.bind("<ButtonRelease-1>", lambda e: "break")
            for _ch in widget.winfo_children():
                _bind_nofocus(_ch, on_press)

        for pname in self.profiles.keys():
            _radio = ctk.CTkRadioButton(dialog, text=pname, variable=prof_var, value=pname)
            _radio.pack(anchor="w", padx=28, pady=2)
            def _on_radio_press(event, _v=pname):
                prof_var.set(_v)
                # Camino del clic idéntico a 18:41: sin medición ni etiqueta
                # diagnóstica (la del 18:53 costaba velocidad aunque el
                # razonamiento dijera que no debía importar).
                try:
                    self._restore_game_window(exe_path, game_hwnd)
                except Exception:
                    pass
                return "break"
            _bind_nofocus(_radio, _on_radio_press)

        def _confirm():
            game_name = name_entry.get().strip() or suggested
            profile = prof_var.get()
            if profile not in self.profiles:
                # Sin perfil activo al arrancar no hay radio preseleccionado:
                # obligar a elegir uno para no guardar un mapeo sin perfil.
                self.show_alert("Elige un perfil", "Selecciona un perfil para el juego.")
                return
            # Extraer icono
            icon_path = os.path.join(GAME_ICONS_DIR, f"{exe_lower}.png")
            if not self.extract_game_icon(exe_path, icon_path):
                icon_path = ""
            self.game_profiles[exe_lower] = {"name": game_name, "profile": profile, "icon": icon_path,
                                             "exe_path": exe_path}
            self.save_game_profiles_to_disk()
            self._selected_game_exe = exe_lower
            self.refresh_game_list_ui()
            # Al agregar, el perfil elegido YA es el activo ("Perfil a activar"):
            # aplicarlo de una vez y marcar el juego como resuelto para que el
            # tick no pregunte de nuevo justo después de agregarlo. La marca
            # de recién agregado sobrevive al reseteo del streak (ver tick).
            self._detect_resolved.add(exe_lower)
            self._just_added_exe = exe_lower
            self._popup_session_exe = exe_lower
            self._auto_switch_profile(profile, exe_lower)
            self._pad_nav = None
            self._pad_nav_prev = None
            dialog.destroy()
            # Si el juego se minimizó (p. ej. al editar el nombre, que toma
            # el foco), devolverlo al frente para que quede "como si nada".
            self._restore_game_window(exe_path, game_hwnd)

        def _cancel():
            self._pad_nav = None
            self._pad_nav_prev = None
            dialog.destroy()
            self._restore_game_window(exe_path, game_hwnd)

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=8)
        # Botones SIN command: el clic se intercepta igual que en los radios
        # para que CTk no ponga el foco y el juego no se minimice.
        _btn_add = ctk.CTkButton(btn_row, text="Agregar", width=120, fg_color="#10b981")
        _btn_add.pack(side="left", padx=8)
        def _on_add_press(event):
            _confirm()
            return "break"
        _bind_nofocus(_btn_add, _on_add_press)
        _btn_cancel = ctk.CTkButton(btn_row, text="Cancelar", width=120, fg_color="#475569")
        _btn_cancel.pack(side="left", padx=8)
        def _on_cancel_press(event):
            _cancel()
            return "break"
        _bind_nofocus(_btn_cancel, _on_cancel_press)

        def _restore_on_dialog_click(ev):
            # Cualquier clic en el cuadro sin manejador propio (etiquetas,
            # zonas vacías) también devuelve el juego al frente al instante.
            # El campo nombre se excluye: ahí el clic toma el foco a
            # propósito para escribir.
            try:
                if ev.widget is name_entry:
                    return
            except Exception:
                pass
            try:
                self._restore_game_window(exe_path, game_hwnd)
            except Exception:
                pass
        dialog.bind("<Button-1>", _restore_on_dialog_click)
        # Navegación por control (sin clics, 0 parpadeos): D-pad mueve
        # entre perfiles, Cross = Agregar, Circle = Cancelar. El nombre
        # se sigue escribiendo con el teclado (o queda el sugerido).
        # El mouse sigue funcionando igual.
        _add_names = list(self.profiles.keys())
        def _nav_move_add(d):
            try:
                if not dialog.winfo_exists():
                    return
                cur = prof_var.get()
                idx = _add_names.index(cur) if cur in _add_names else (-1 if d > 0 else 0)
                prof_var.set(_add_names[(idx + d) % len(_add_names)])
            except Exception:
                pass
        def _nav_accept_add():
            try:
                if dialog.winfo_exists():
                    _confirm()
            except Exception:
                pass
        def _nav_cancel_add():
            try:
                if dialog.winfo_exists():
                    _cancel()
            except Exception:
                pass
        self._pad_nav = {"dialog": dialog, "move": _nav_move_add, "accept": _nav_accept_add, "cancel": _nav_cancel_add}
        self._pad_nav_prev = None

    def _subclass_noactivate(self, dialog):
        """Evita que el clic active la ventana: el wndproc de Tk ignora
        WS_EX_NOACTIVATE y responde MA_ACTIVATE a WM_MOUSEACTIVATE, y eso le
        quita el primer plano al juego y lo minimiza (por eso el clic en una
        opción minimizaba aunque el cuadro ya era NOACTIVATE). Devolviendo
        MA_NOACTIVATE el clic llega pero la ventana jamás toma el foco.
        Best-effort: si falla, el cuadro sigue funcionando como antes."""
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = dialog.winfo_id()
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                               wintypes.WPARAM, wintypes.LPARAM]
            WM_MOUSEACTIVATE = 0x0021
            WM_NCDESTROY = 0x0082
            MA_NOACTIVATE = 3
            GWLP_WNDPROC = -4
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                         wintypes.WPARAM, wintypes.LPARAM)
            old = {"proc": None}

            def _proc(h, msg, wp, lp):
                if msg == WM_MOUSEACTIVATE:
                    return MA_NOACTIVATE
                if msg == WM_NCDESTROY and old["proc"]:
                    try:
                        user32.SetWindowLongPtrW(h, GWLP_WNDPROC, old["proc"])
                    except Exception:
                        pass
                try:
                    return user32.CallWindowProcW(old["proc"], h, msg, wp, lp)
                except Exception:
                    return 0

            cb = WNDPROC(_proc)
            prev = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, cb)
            if not prev:
                return False
            old["proc"] = prev
            # Mantener vivo el callback: si el GC lo borra, la ventana se rompe.
            dialog._noactivate_cb = cb
            dialog._noactivate_old = prev
            return True
        except Exception:
            return False

    def _restore_game_window(self, exe_path, game_hwnd=None):
        """Devuelve al frente la ventana del juego si se minimizó al
        interactuar con un cuadro. Primero la ventana guardada al capturar
        (la más fiable). BUILD DE PRUEBA: si se pasó un hwnd guardado, NO
        se busca por exe aunque no valga (la búsqueda es la sospecha del
        parpadeo lento); el caso se registra en restore_log.txt. Si
        game_hwnd es None, se mantiene la búsqueda como red de seguridad."""
        try:
            _log_base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        except Exception:
            _log_base = None
        try:
            if game_hwnd and win32gui.IsWindow(game_hwnd):
                # SetForegroundWindow SIEMPRE (sin atajo si ya está al
                # frente): el atajo del build 18:53 alargó el parpadeo en
                # vez de quitarlo (revertido 19:05 — el reorden 18:57 no
                # cambió nada, así que el atajo era la diferencia).
                # Reafirmar el frente acorta el parpadeo al mínimo.
                try:
                    if win32gui.IsIconic(game_hwnd):
                        win32gui.ShowWindow(game_hwnd, 9)  # SW_RESTORE
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(game_hwnd)
                    if _log_base:
                        try:
                            with open(os.path.join(_log_base, "restore_log.txt"), "a", encoding="utf-8") as _f:
                                _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} FAST exe={exe_path} hwnd={game_hwnd}\n")
                        except Exception:
                            pass
                    return
                except Exception:
                    pass
        except Exception:
            pass
        # BUILD DE PRUEBA: con hwnd guardado, no buscar por exe.
        if game_hwnd:
            if _log_base:
                try:
                    with open(os.path.join(_log_base, "restore_log.txt"), "a", encoding="utf-8") as _f:
                        _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SKIP-FALLBACK exe={exe_path} hwnd={game_hwnd}\n")
                except Exception:
                    pass
            return
        if not exe_path:
            return
        try:
            target = exe_path.lower()
            found = []

            def _enum(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    _, pid = win32gui.GetWindowThreadProcessId(hwnd)
                    if not pid:
                        return True
                    if psutil.Process(pid).exe().lower() == target:
                        found.append(hwnd)
                        return False  # parar
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_enum, None)
            if not found:
                return
            hwnd = found[0]
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            except Exception:
                pass
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        except Exception:
            pass

    def _auto_switch_profile(self, profile_name, exe):
        """Cambia el perfil por detección de juego (hilo principal)."""
        if profile_name in self.profiles and self.current_profile_name != profile_name:
            self.load_profile_to_ui(profile_name)
            self.profile_combo.set(profile_name)
        self._last_auto_game = exe
        # Mostrar qué juego se detectó
        try:
            if exe and exe in self.game_profiles:
                self.game_detect_label.configure(
                    text=f"▶ {self.game_profiles[exe].get('name', exe)}", text_color="#4ade80")
            else:
                self.game_detect_label.configure(text="", text_color="#4ade80")
        except Exception:
            pass

    def _prompt_game_detected(self, exe, target):
        """Popup al frente cuando se detecta un juego mapeado.
        Abre 15s después de detectado (para no aparecer sobre Javelin/launcher).
        Aparece cada vez que el juego abre (cada sesión al frente), aunque
        su perfil ya esté puesto, NUNCA justo después de agregarlo (eso lo
        cubre el cuadro de Agregar). Muestra el perfil asignado y deja
        elegir otro: radios con todos los perfiles en vivo, con el asignado
        preseleccionado. Aplicar pone el elegido; Cancelar/X no cambia nada.
        En ningún caso vuelve a preguntar mientras ese juego siga al frente.
        Sin grab_set y SIN robar el foco (WS_EX_NOACTIVATE): el cuadro se
        dibuja encima y recibe clics del mouse sin quitarle el primer plano
        al juego, así que al Aceptar solo se cierra el cuadro y el juego
        queda al frente (con focus_force Windows minimizaba el juego).
        Sin foco no responde al teclado, solo al mouse: es lo buscado.
        Si juegas en pantalla completa exclusiva el cuadro puede quedar
        detrás (Alt+Tab lo muestra), en modo ventana sin bordes sí aparece encima."""
        if getattr(self, '_detect_dialog', None) is not None:
            return  # ya hay un popup abierto
        if exe in getattr(self, '_detect_resolved', set()):
            return  # ya se resolvió (aceptado o ignorado) con este juego al frente
        # Ventana del juego AHORA (está al frente cuando el tick detecta):
        # para devolverla al frente al cerrar el popup.
        try:
            popup_game_hwnd = win32gui.GetForegroundWindow()
        except Exception:
            popup_game_hwnd = None
        popup_exe_path = self.game_profiles.get(exe, {}).get("exe_path") or exe
        game_name = self.game_profiles.get(exe, {}).get('name', exe)
        dialog = ctk.CTkToplevel(self)
        self._detect_dialog = dialog
        self._popup_session_exe = exe
        dialog.title("Juego detectado")
        # Opciones: todos los perfiles en vivo. Preseleccionado = el perfil
        # asignado al juego (o el primero si ya no existe).
        radio_names = list(self.profiles.keys())
        preselected = target if target in self.profiles else (radio_names[0] if radio_names else "")
        dialog.geometry(f"400x{250 + 32 * len(radio_names)}")
        dialog.attributes("-topmost", True)
        try:
            dialog.lift()
        except Exception:
            pass

        ctk.CTkLabel(dialog, text="🎮 Juego detectado", font=("Segoe UI", 14, "bold"),
                     text_color="#4ade80").pack(pady=(16, 4))
        ctk.CTkLabel(dialog, text=f"{game_name}\n({exe})", font=("Segoe UI", 12),
                     wraplength=340).pack(pady=4)
        _profile_line = f"Perfil asignado: {target}"
        if self.current_profile_name == target:
            _profile_line += " (ya aplicado)"
        ctk.CTkLabel(dialog, text=_profile_line, font=("Segoe UI", 11),
                     text_color="#93c5fd", wraplength=340).pack(pady=(0, 4))
        ctk.CTkLabel(dialog, text="¿Con qué perfil quieres jugar?", font=("Segoe UI", 11, "bold"),
                     wraplength=340).pack(pady=(0, 4))
        # Radios DENTRO del cuadro (igual que en Agregar): cambiar de perfil
        # sin salir del cuadro. El clic solo marca y devuelve el juego al
        # frente al instante; NADA más en el camino del clic (la lección del
        # diagnóstico: cualquier trabajo extra ahí alarga el parpadeo).
        prof_var = ctk.StringVar(value=preselected)
        def _bind_nofocus(widget, on_press):
            widget.bind("<Button-1>", on_press)
            widget.bind("<ButtonRelease-1>", lambda e: "break")
            for _ch in widget.winfo_children():
                _bind_nofocus(_ch, on_press)
        for pname in radio_names:
            _radio = ctk.CTkRadioButton(dialog, text=pname, variable=prof_var, value=pname)
            _radio.pack(anchor="w", padx=28, pady=2)
            def _on_radio_press(event, _v=pname):
                prof_var.set(_v)
                try:
                    self._restore_game_window(popup_exe_path, popup_game_hwnd)
                except Exception:
                    pass
                return "break"
            _bind_nofocus(_radio, _on_radio_press)

        def _close(action, selected=None):
            self._detect_resolved.add(exe)
            self._detect_dialog = None
            self._pad_nav = None
            self._pad_nav_prev = None
            try:
                dialog.destroy()
            except Exception:
                pass
            # Si el clic minimizó el juego (o al cerrar quedó la ventana
            # principal al frente), devolver el juego al frente ANTES de
            # aplicar: el trabajo de aplicar no debe retrasar el regreso.
            try:
                self._restore_game_window(popup_exe_path, popup_game_hwnd)
            except Exception:
                pass
            if action == "apply" and selected:
                if selected in self.profiles:
                    self._auto_switch_profile(selected, exe)
                # Si el elegido ya no existe, no se cambia nada (solo resuelto).
            else:
                # Cancelar/X: no cambia nada, pero marca el juego como visto
                # para que el tick no vuelva a preguntar mientras siga al frente.
                self._last_auto_game = exe
                try:
                    self.game_detect_label.configure(text=f"▶ {game_name}", text_color="#4ade80")
                except Exception:
                    pass

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=8)
        _btn_ok = ctk.CTkButton(btn_row, text="Aplicar", width=120, fg_color="#10b981")
        _btn_ok.pack(side="left", padx=8)
        def _on_ok_press(event):
            _close("apply", prof_var.get())
            return "break"
        _bind_nofocus(_btn_ok, _on_ok_press)
        _btn_no = ctk.CTkButton(btn_row, text="Cancelar", width=120, fg_color="#475569")
        _btn_no.pack(side="left", padx=8)
        def _on_no_press(event):
            _close("cancel")
            return "break"
        _bind_nofocus(_btn_no, _on_no_press)
        # NO robar el foco: el focus_force anterior cambiaba el primer plano
        # y Windows minimizaba el juego. WS_EX_NOACTIVATE deja que el cuadro
        # reciba clics SIN quitarle el foco al juego: al Aceptar solo se
        # cierra el cuadro y el juego queda al frente.
        try:
            hwnd = dialog.winfo_id()
            exstyle = win32gui.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            win32gui.SetWindowLongW(hwnd, -20, exstyle | 0x08000000)  # WS_EX_NOACTIVATE
        except Exception:
            pass
        # Además: subclass para que el clic no active la ventana (igual que en Agregar).
        self._subclass_noactivate(dialog)
        # Cerrar con la X = Cancelar: no cambia nada.
        dialog.protocol("WM_DELETE_WINDOW", lambda: _close("cancel"))
        # Auto-cierre a los 20 s (pedido por Pedro): si no elige, se cierra
        # y queda el perfil que entró (igual que Cancelar, sin cambiar nada).
        def _auto_close_popup():
            try:
                if self._detect_dialog is dialog and dialog.winfo_exists():
                    _close("cancel")
            except Exception:
                pass
        dialog.after(20000, _auto_close_popup)
        ctk.CTkLabel(dialog, text="Se cierra solo en 20 s si no eliges (queda el perfil aplicado).",
                     font=("Segoe UI", 10), text_color="#64748b", wraplength=340).pack(pady=(0, 6))
        # Navegación por control (sin clics, 0 parpadeos): D-pad mueve,
        # Cross aplica, Circle cancela. El mouse sigue funcionando igual.
        def _nav_move(d):
            try:
                if not dialog.winfo_exists():
                    return
                cur = prof_var.get()
                idx = radio_names.index(cur) if cur in radio_names else (-1 if d > 0 else 0)
                prof_var.set(radio_names[(idx + d) % len(radio_names)])
            except Exception:
                pass
        def _nav_accept():
            try:
                if dialog.winfo_exists():
                    _close("apply", prof_var.get())
            except Exception:
                pass
        def _nav_cancel():
            try:
                if dialog.winfo_exists():
                    _close("cancel")
            except Exception:
                pass
        self._pad_nav = {"dialog": dialog, "move": _nav_move, "accept": _nav_accept, "cancel": _nav_cancel}
        self._pad_nav_prev = None

    @staticmethod
    def _running_process_names():
        """Nombres (minúsculas) de los procesos activos, en una sola pasada.
        La detección por proceso usa esto: el .exe activo manda, no el foco."""
        try:
            import psutil
            return {(p.info.get("name") or "").lower()
                    for p in psutil.process_iter(["name"])}
        except Exception:
            return set()

    def _clear_pending_popup(self):
        """Descarta el recuadro demorado pendiente (sesión terminada)."""
        self._pending_popup_exe = None
        self._pending_popup_target = None
        self._pending_popup_due = None

    def _end_popup_session(self):
        """Termina la sesión del popup: borra lo resuelto de ese juego,
        cierra su cuadro si está abierto y limpia la navegación por control.
        La sesión solo termina así (proceso muerto o ausencia de 10s); una
        pérdida transitoria de foco NO la termina (eso causaba la repregunta
        cada pocos segundos)."""
        try:
            _sess = getattr(self, "_popup_session_exe", None)
            if _sess:
                self._detect_resolved.discard(_sess)
                if getattr(self, "_just_added_exe", None) == _sess:
                    self._just_added_exe = None
            self._clear_pending_popup()
            self._popup_session_exe = None
            self._popup_absent_since = None
            if getattr(self, "_detect_dialog", None) is not None:
                try:
                    self._detect_dialog.destroy()
                except Exception:
                    pass
                self._detect_dialog = None
            self._pad_nav = None
            self._pad_nav_prev = None
        except Exception:
            pass

    def _check_game_detection(self):
        """Máquina de estados de juegos (tick cada 1s):
        Detección por PROCESO (el .exe activo manda, no el foco) o por ventana
        al frente. El perfil se aplica al detectar; el recuadro de confirmación
        abre 15s después para no aparecer sobre Javelin/launcher.
        - VIGILANCIA: ningún juego aplicado; espera a que un juego mapeado esté activo.
        - APLICADO: el juego mapeado tiene su perfil puesto; no se suelta por
          perder el foco mientras su proceso siga vivo.
        - AUSENCIA: ningún exe mapeado activo; si pasan 5 segundos sin ver
          ninguno, suelta a Gatillos Suaves y vuelve a VIGILANCIA.
        Aplicar por foco tiene antirrebote: el mismo exe 2 ticks seguidos.
        Revertir usa tiempo real (timestamp), no conteo de ticks, para que sean 10s de verdad.
        Si el usuario cambió el perfil a mano, el revertir no lo pisa (respeta el manual)."""
        exe_lower, _ = self.get_foreground_exe()
        # Detección por PROCESO (Pedro: el .exe activo manda, no el foco):
        # un juego mapeado que está corriendo se detecta aunque otra ventana
        # tenga el foco (al abrir, Javelin/launcher se quedan con el foco y
        # el juego en pantalla completa no lo recibe hasta el clic). Una sola
        # pasada de procesos por tick. También cubre el RELEVO launcher→juego:
        # si el aplicado murió pero otro mapeado vive, se cambia a ese sin
        # soltar el perfil (el hueco de ~1s entre ambos no revierte nada).
        _names = self._running_process_names()
        self._last_seen_process_names = _names
        if not self._last_auto_game or self._last_auto_game not in _names:
            for _exe in self.game_profiles:
                if _exe in _names:
                    exe_lower = _exe
                    self._detect_game_streak = _exe
                    self._detect_game_count = 2
                    break
        if exe_lower and exe_lower in self.game_profiles:
            # Juego mapeado al frente: presente de nuevo, se cancela cualquier ausencia.
            self._absence_since = None
            # Sesión del popup: si al frente hay OTRO juego mapeado distinto
            # al de la sesión, esa sesión terminó (su resuelto se borra y su
            # cuadro se cierra). Si es el mismo juego, su ausencia termina.
            _sess = getattr(self, "_popup_session_exe", None)
            if _sess and _sess != exe_lower:
                self._end_popup_session()
            elif _sess == exe_lower:
                self._popup_absent_since = None
            # Antirrebote hacia el juego (2 ticks seguidos al frente).
            if exe_lower == self._detect_game_streak:
                self._detect_game_count += 1
            else:
                self._detect_game_streak = exe_lower
                self._detect_game_count = 1
                # NO borrar _detect_resolved aquí: antes, cualquier pérdida
                # transitoria de foco (un clic que minimiza el juego, un
                # Alt+Tab) reseteaba el streak y borraba lo resuelto, y el
                # popup volvía cada pocos segundos. La sesión solo termina
                # por proceso muerto o ausencia de 10s (rama else). El caso
                # "recién agregado" ya está cubierto porque _confirm marca
                # resuelto y fija la sesión.
            if self._detect_game_count < 2:
                return
            target = self.game_profiles[exe_lower].get("profile", "Gatillos Suaves (Estándar)")
            # Si el perfil mapeado ya no existe (borrado), caer al estándar.
            if target not in self.profiles:
                target = "Gatillos Suaves (Estándar)"
            # Abrir CON el perfil mapeado desde el inicio (sin esperar la
            # confirmación del popup): así un juego anti-cheat arranca ya
            # con su postura. El popup sigue ofreciendo confirmar/cambiar
            # en la sesión; si el perfil ya está puesto, esto no hace nada.
            try:
                self._auto_switch_profile(target, exe_lower)
            except Exception:
                pass
            # Confirmación DEMORADA 15s: el perfil ya se aplicó arriba, pero
            # el recuadro espera para no aparecer sobre Javelin/launcher.
            # Se agenda una sola vez por sesión; si la sesión termina antes
            # (juego cerrado, 10s ausente), _end_popup_session lo descarta.
            if exe_lower not in self._detect_resolved and self._pending_popup_exe != exe_lower:
                self._pending_popup_exe = exe_lower
                self._pending_popup_target = target
                self._pending_popup_due = time.time() + 15.0
            if (self._pending_popup_exe == exe_lower
                    and self._pending_popup_due is not None
                    and time.time() >= self._pending_popup_due
                    and exe_lower not in self._detect_resolved):
                _exe, _tgt = self._pending_popup_exe, self._pending_popup_target
                self._clear_pending_popup()
                try:
                    self.after(0, lambda: self._prompt_game_detected(_exe, _tgt))
                except Exception:
                    pass
        else:
            self._detect_game_streak = None
            self._detect_game_count = 0
            # Sesión del popup: una pérdida transitoria de foco NO la
            # termina (eso causaba la repregunta). Solo termina si el
            # proceso del juego murió o si la ausencia llega a 10s.
            _sess = getattr(self, "_popup_session_exe", None)
            if _sess:
                _gone = False
                try:
                    _gone = not any((p.info.get("name") or "").lower() == _sess
                                    for p in psutil.process_iter(["name"]))
                except Exception:
                    _gone = False
                if _gone:
                    self._end_popup_session()
                else:
                    _now = time.time()
                    if getattr(self, "_popup_absent_since", None) is None:
                        self._popup_absent_since = _now
                    elif _now - self._popup_absent_since >= self._ABSENCE_TIMEOUT:
                        self._end_popup_session()
            if not self._last_auto_game:
                # VIGILANCIA: ningún juego aplicado, nada que soltar.
                self._absence_since = None
                return
            # Si el proceso del juego aplicado sigue VIVO, el perfil NO se
            # suelta por no tener el foco (Alt+Tab, Javelin, launcher):
            # solo se suelta cuando ningún exe mapeado lleva 5s sin verse.
            if self._last_auto_game in _names:
                self._absence_since = None
                return
            # APLICADO pero no al frente: medir la ausencia en segundos reales.
            now = time.time()
            if self._absence_since is None:
                self._absence_since = now
                return
            if now - self._absence_since < self._REVERT_GRACE:
                return
            # Sin ningún exe mapeado >= 5s: soltar a Gatillos Suaves y volver a VIGILANCIA.
            self._absence_since = None
            self._clear_pending_popup()
            self._just_added_exe = None  # el juego se fue de verdad: próxima apertura sí pregunta
            last_exe = self._last_auto_game
            last_profile = self.game_profiles.get(last_exe, {}).get("profile")
            if self.current_profile_name == last_profile:
                try:
                    self.after(0, lambda: self._auto_switch_profile("Gatillos Suaves (Estándar)", None))
                except Exception:
                    pass
            else:
                self._last_auto_game = None  # El usuario tomó control manual

    def _apply_profile_posture(self, profile_name, p):
        """La postura emulación+cloak la manda el perfil que se carga,
        siempre y sin excepciones por el camino que traiga el usuario:
        - Anti-cheat: su postura guardada (Solo Triggers, cloak apagado).
        - Creado por el usuario (con hidhide_active guardado): su postura
          guardada, absolutamente toda.
        - Predeterminado: la postura de serie, PS4 + cloak ENCENDIDO,
          aunque el usuario lo haya apagado a mano o en otro perfil.
        - Creado con el código viejo (sin hidhide_active): no se toca,
          como antes."""
        if p.get("applies_posture") or (
            profile_name not in DEFAULT_PROFILES and "hidhide_active" in p
        ):
            emu = p.get("emulation", "DualShock 4 (PS4)")
            if emu not in ("Xbox 360 (XInput)", "DualShock 4 (PS4)", "Solo Triggers (Sin Emular)"):
                emu = "DualShock 4 (PS4)"
            cloak_on = bool(p.get("hidhide_active", True))
        elif profile_name in DEFAULT_PROFILES:
            emu = "DualShock 4 (PS4)"
            cloak_on = True
        else:
            return  # legado: no tocar la postura
        self.emu_selector.set(emu)
        if self.running_thread:
            self.setup_virtual_controller(emu)
        if cloak_on:
            self.hidhide_switch.select()
            if self.hw.connected:
                self._enable_cloak()
        else:
            self.hidhide_switch.deselect()
            self.hidcloak.disable()

    def load_profile_to_ui(self, profile_name, apply_posture=True):
        self._loading_profile = True
        self._sticks_dirty = False
        # Cargar un perfil = activarlo (selección manual, juego mapeado o
        # revertir a Gatillos Suaves tras una ausencia).
        self._profile_active = True
        try:
            self.current_profile_name = profile_name
            p = self.profiles[profile_name]

            # Postura emulación+cloak: la manda el perfil que se carga.
            # Predeterminado = PS4 + cloak encendido, siempre; Anti-cheat
            # y creados por el usuario = su postura guardada.
            # El cambio rápido pasa apply_posture=False: solo sensación,
            # para no rearmar PS4+cloak en pleno Anti-cheat.
            if apply_posture:
                self._apply_profile_posture(profile_name, p)

            l2_m = p.get("l2_mode", "Desactivado")
            r2_m = p.get("r2_mode", "Desactivado")
            if l2_m not in TRIGGER_MODES_LIST:
                l2_m = "Personalizado (Avanzado)"
            if r2_m not in TRIGGER_MODES_LIST:
                r2_m = "Personalizado (Avanzado)"

            self.l2_controls["mode"].set(l2_m)
            self.r2_controls["mode"].set(r2_m)

            for k in ["start", "end", "force"]:
                v_l = p.get(f"l2_{k}", 0)
                self.l2_controls["sliders"][k]["slider"].set(v_l)
                self.l2_controls["sliders"][k]["val_lbl"].configure(text=str(int(v_l)))

                v_r = p.get(f"r2_{k}", 0)
                self.r2_controls["sliders"][k]["slider"].set(v_r)
                self.r2_controls["sliders"][k]["val_lbl"].configure(text=str(int(v_r)))

            self.update_trigger_sliders_visibility(self.l2_controls, l2_m)
            self.update_trigger_sliders_visibility(self.r2_controls, r2_m)

            dz_l, dz_r, curve = self._resolve_sticks(p)
            sens = p.get("mouse_sens", 1.5)

            self.dz_l_slider.set(dz_l)
            self.dz_l_lbl.configure(text=f"L3: {int(dz_l)}%")
            self.dz_r_slider.set(dz_r)
            self.dz_r_lbl.configure(text=f"R3: {int(dz_r)}%")
            self.curve_slider.set(curve)
            self.curve_lbl.configure(text=f"Exponente: {curve:.1f}")
            self.sens_slider.set(sens)
            self.sens_lbl.configure(text=f"Velocidad: {sens:.1f}x")

            # Ratón táctil GLOBAL (pedido por Pedro): NO va con el perfil.
            # El interruptor es independiente (studio_settings.json) y
            # ningún perfil lo cambia: antes, al aplicar un perfil con
            # touchpad_mouse=True, el ratón se encendía solo.

            if self.running_thread:
                # FIX v2.4: NO recrear el mando virtual aquí. La emulación es global
                # y no cambia con el perfil; recrearlo hacía que el juego viera
                # el mando desconectarse/reconectarse ("se vuelve loco").
                self.apply_triggers_to_hardware()
        finally:
            self._loading_profile = False

    def _resolve_quick_switch_map(self, exe=None):
        """Set de flechas vigente: el personalizado del juego activo, si lo
        tiene; si no, el ajuste general (QUICK_SWITCH_MAP). Cada dirección
        con un nombre ya inexistente cae a su valor general."""
        merged = dict(QUICK_SWITCH_MAP)
        if exe:
            qs = (self.game_profiles.get(exe) or {}).get("quick_switch") or {}
            for _d in ("up", "down", "left", "right"):
                if qs.get(_d) in self.profiles:
                    merged[_d] = qs[_d]
        return merged

    def _quick_switch_profile(self, profile_name):
        """Cambio rápido dentro del juego (hilo principal vía after(0)).
        SOLO GATILLOS: copia el modo y los 3 valores de L2/R2 del perfil
        elegido a los controles y al mando. NO toca emulación, cloak,
        sticks, velocidad del touchpad, ni el perfil actual (el combo
        sigue mostrando el perfil de la sesión): en Anti-cheat la sesión
        sigue siendo Anti-cheat aunque la sensación venga de otro perfil."""
        if profile_name not in self.profiles:
            return
        p = self.profiles[profile_name]
        self._loading_profile = True
        try:
            l2_m = p.get("l2_mode", "Desactivado")
            r2_m = p.get("r2_mode", "Desactivado")
            if l2_m not in TRIGGER_MODES_LIST:
                l2_m = "Personalizado (Avanzado)"
            if r2_m not in TRIGGER_MODES_LIST:
                r2_m = "Personalizado (Avanzado)"

            self.l2_controls["mode"].set(l2_m)
            self.r2_controls["mode"].set(r2_m)

            for k in ["start", "end", "force"]:
                v_l = p.get(f"l2_{k}", 0)
                self.l2_controls["sliders"][k]["slider"].set(v_l)
                self.l2_controls["sliders"][k]["val_lbl"].configure(text=str(int(v_l)))

                v_r = p.get(f"r2_{k}", 0)
                self.r2_controls["sliders"][k]["slider"].set(v_r)
                self.r2_controls["sliders"][k]["val_lbl"].configure(text=str(int(v_r)))

            self.update_trigger_sliders_visibility(self.l2_controls, l2_m)
            self.update_trigger_sliders_visibility(self.r2_controls, r2_m)

            if self.running_thread:
                self.apply_triggers_to_hardware()
        finally:
            self._loading_profile = False

    def collect_ui_state(self):
        _sw = getattr(self, "hidhide_switch", None)
        return {
            "emulation": self.emu_selector.get(),
            "hidhide_active": bool(_sw.get()) if _sw is not None else True,
            "l2_mode": self.l2_controls["mode"].get(),
            "l2_start": int(self.l2_controls["sliders"]["start"]["slider"].get()),
            "l2_end": int(self.l2_controls["sliders"]["end"]["slider"].get()),
            "l2_force": int(self.l2_controls["sliders"]["force"]["slider"].get()),
            "r2_mode": self.r2_controls["mode"].get(),
            "r2_start": int(self.r2_controls["sliders"]["start"]["slider"].get()),
            "r2_end": int(self.r2_controls["sliders"]["end"]["slider"].get()),
            "r2_force": int(self.r2_controls["sliders"]["force"]["slider"].get()),
            "deadzone_l": int(self.dz_l_slider.get()),
            "deadzone_r": int(self.dz_r_slider.get()),
            "curve": round(float(self.curve_slider.get()), 2),
            "sticks_customized": bool(
                self._sticks_dirty
                or (self.profiles.get(self.current_profile_name) or {}).get("sticks_customized")
            ),
            "mouse_sens": round(float(self.sens_slider.get()), 2),
            "lightbar": list(self.lightbar_color)  # FIX 9: desacoplado del perfil
        }

    def save_current_profile(self, confirm=False):
        """Guardar EXPLÍCITO (botón Guardar): el único que persiste.
        - Serie: solo gatillos/zonas/curva/velocidad; emulación y cloak
          NO se reescriben (se conserva lo guardado del perfil).
        - Creado por el usuario: guarda ABSOLUTAMENTE TODO, emulación y
          cloak incluidos.
        confirm=True (solo el botón) muestra el recuadro 'Guardado'."""
        if not self.current_profile_name:
            self.show_alert("Ningún perfil seleccionado",
                            "Elige un perfil o crea uno nuevo primero.")
            return
        data = self.collect_ui_state()
        data.pop("lightbar", None)  # FIX 9: la luz ya no vive en el perfil
        if self.current_profile_name in DEFAULT_PROFILES:
            prev = self.profiles.get(self.current_profile_name, {})
            if "emulation" in prev:
                data["emulation"] = prev["emulation"]
            if "hidhide_active" in prev:
                data["hidhide_active"] = prev["hidhide_active"]
        self.profiles[self.current_profile_name] = data
        self.save_profiles_to_disk()
        self.apply_triggers_to_hardware()
        if confirm:
            self.show_alert("Guardado", f"Perfil '{self.current_profile_name}' guardado.")

    # FIX 9: color de luz propio (studio_settings.json)
    def load_settings(self):
        self._settings_touchpad_mouse = False
        self._settings_backup = True
        self._update_snooze_until = 0.0
        self._update_snooze_version = ""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("touchpad_mouse", False):
                    self._settings_touchpad_mouse = True
                self._settings_backup = bool(data.get("backup_enabled", True))
                try:
                    self._update_snooze_until = float(data.get("update_snooze_until") or 0)
                except Exception:
                    self._update_snooze_until = 0.0
                self._update_snooze_version = str(data.get("update_snooze_version") or "")
                lb = data.get("lightbar")
                if isinstance(lb, (list, tuple)) and len(lb) == 3:
                    return [max(0, min(255, int(c))) for c in lb]
            except Exception:
                pass
        return [0, 130, 255]

    def save_settings_to_disk(self):
        data = {}
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
        except Exception:
            data = {}
        data["lightbar"] = self.lightbar_color
        data["backup_enabled"] = bool(getattr(self, "_settings_backup", True))
        _sw = getattr(self, "touch_switch", None)
        if _sw is not None:
            try:
                data["touchpad_mouse"] = bool(_sw.get())
            except Exception:
                pass
        self._atomic_json_write(SETTINGS_FILE, data)

    def _refresh_color_btn(self):
        r, g, b = self.lightbar_color
        hexcolor = "#%02x%02x%02x" % (r, g, b)
        # texto legible segun luminosidad
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        self.color_btn.configure(fg_color=hexcolor,
                                 text_color="#0f172a" if lum > 0.6 else "#ffffff")

    def set_lightbar_preset(self, rgb):
        """Setea el color directo sin diálogo (diagnóstico + fallback)."""
        self.lightbar_color = [int(rgb[0]), int(rgb[1]), int(rgb[2])]
        self.save_settings_to_disk()
        self._refresh_color_btn()
        self.apply_triggers_to_hardware()

    def pick_lightbar_color(self):
        from tkinter import colorchooser
        initial = "#%02x%02x%02x" % tuple(self.lightbar_color)
        # CORREGIDO: askcolor retorna (rgb_tuple, hex_string), no al revés
        rgb_tuple, _hex = colorchooser.askcolor(color=initial, title="Color de la luz del mando")
        if rgb_tuple:
            self.lightbar_color = [int(rgb_tuple[0]), int(rgb_tuple[1]), int(rgb_tuple[2])]
            self.save_settings_to_disk()
            self._refresh_color_btn()
            self.apply_triggers_to_hardware()

    @staticmethod
    def _batt_text(st):
        lvl = st.get("batt_level")
        if lvl is None:
            return ""
        if st.get("batt_charging"):
            return f"Bateria {lvl}% (cargando)"
        return f"Bateria {lvl}%" 

    def create_new_profile(self):
        name = self.new_profile_entry.get().strip()
        if not name:
            return
        if name in self.profiles:
            self.show_alert("Ya existe", f"Ya hay un perfil llamado '{name}'.")
            return
        self.profiles[name] = self.collect_ui_state()
        self.save_profiles_to_disk()
        self.profile_combo.configure(values=list(self.profiles.keys()))
        self.profile_combo.set(name)
        self.current_profile_name = name
        self._profile_active = True  # crear = elegirlo explícitamente
        self.new_profile_entry.delete(0, "end")
        self.refresh_game_list_ui()
        if self.tray_icon:
            self.tray_icon.menu = self.build_tray_menu()

    def on_emulation_change(self, new_mode):
        if self.running_thread:
            self.setup_virtual_controller(new_mode)

    def setup_virtual_controller(self, mode_str):
        # FIX 4: sin ViGEmBus no crashea; avisa y cae a Solo Triggers
        self.virtual_pad = None
        self.rumble_large = 0
        self.rumble_small = 0
        try:
            if mode_str == "Xbox 360 (XInput)":
                self.virtual_pad = vg.VX360Gamepad()
                self.emulation_type = "X360"
            elif mode_str == "DualShock 4 (PS4)":
                self.virtual_pad = vg.VDS4Gamepad()
                self.emulation_type = "DS4"
            else:
                self.emulation_type = "NONE"
                return
            # FIX 5: rumble real via notificacion (0-255, sin divisiones raras)
            self.virtual_pad.register_notification(callback_function=self._on_rumble)
        except Exception as e:
            self.emulation_type = "NONE"
            self.emu_selector.set("Solo Triggers (Sin Emular)")
            self.show_alert("Falta ViGEmBus",
                            "No se pudo crear el control virtual.\n\n"
                            "Instala el driver ViGEmBus:\n"
                            f"{VIGEMBUS_URL}\n\n"
                            f"Detalle: {e}\n\n"
                            "Mientras tanto: modo Solo Triggers.")

    def apply_triggers_to_hardware(self):
        if not self.hw.connected:
            return
        st = self.collect_ui_state()
        if not getattr(self, "_profile_active", True):
            # Sin perfil activo: gatillos desactivados, pero rumble y luz siguen vivos.
            st = dict(st)
            st["l2_mode"] = "Desactivado"
            st["r2_mode"] = "Desactivado"
        l2_cfg = {"mode": st["l2_mode"], "start": st["l2_start"], "end": st["l2_end"],
                  "force": st["l2_force"]}
        r2_cfg = {"mode": st["r2_mode"], "start": st["r2_start"], "end": st["r2_end"],
                  "force": st["r2_force"]}
        self._debug_log_write(f"APPLY rumble=({self.rumble_small},{self.rumble_large}) "
                              f"r2={r2_cfg['mode']} l2={l2_cfg['mode']}")
        self.hw.send_packet(r2_cfg, l2_cfg, st["lightbar"],
                            self.rumble_small, self.rumble_large)
        # marca lo ultimo enviado para el throttle del loop
        self._last_sig = (st["l2_mode"], st["l2_start"], st["l2_end"], st["l2_force"],
                          st["r2_mode"], st["r2_start"], st["r2_end"], st["r2_force"],
                          tuple(st["lightbar"]), self.rumble_small, self.rumble_large)

    def toggle_service(self):
        if self.running_thread:
            self.stop_service()
        else:
            self.start_service()

    def start_service(self):
        if self.hw.connect():
            self.setup_virtual_controller(self.emu_selector.get())
            self.running_thread = True
            # Anti doble-input: cloak en HidHide (disable() restaura tu config al salir)
            if self.hidhide_switch.get():
                self._enable_cloak()
            self.apply_triggers_to_hardware()

            threading.Thread(target=self.worker_loop, daemon=True).start()

            conn_mode = "Bluetooth (CRC32)" if self.hw.is_bluetooth else "USB Directo"
            self.status_label.configure(text=f"● Conectado: {conn_mode}", text_color="#4ade80")
            self.connect_btn.configure(text="Desconectar", fg_color="#ef4444", hover_color="#dc2626")
        else:
            self.status_label.configure(text="● Mando no encontrado", text_color="#f87171")

    def stop_service(self):
        self.running_thread = False
        self.hidcloak.disable()  # limpia el cloak y restaura tu config previa
        self.hw.close()
        self.virtual_pad = None
        self.rumble_large = 0
        self.rumble_small = 0
        self._last_sig = None
        self.status_label.configure(text="● Desconectado", text_color="#f87171")
        self.connect_btn.configure(text="Conectar Mando", fg_color="#3b82f6", hover_color="#2563eb")
        self.batt_label.configure(text="")
        self._last_batt_txt = ""

    @staticmethod
    def process_axis(raw_byte, deadzone_pct, curve_exp, invert=False):
        norm = (raw_byte - 128.0) / 127.0
        if invert:
            norm = -norm
        dz = deadzone_pct / 100.0
        mag = abs(norm)
        if mag <= dz:
            return 0.0
        scaled = (mag - dz) / (1.0 - dz)
        scaled = min(1.0, max(0.0, scaled)) ** curve_exp
        return scaled if norm > 0 else -scaled

    def handle_touchpad_mouse(self, st, sens):
        if st["touch_active"]:
            if self.last_touch_active:
                dx = int((st["touch_x"] - self.last_tx) * sens * 0.65)
                dy = int((st["touch_y"] - self.last_ty) * sens * 0.65)
                if abs(dx) < 120 and abs(dy) < 120:
                    ctypes.windll.user32.mouse_event(MOUSEEVENTF_MOVE, dx, dy, 0, 0)
            self.last_tx = st["touch_x"]
            self.last_ty = st["touch_y"]
            self.last_touch_active = True
        else:
            self.last_touch_active = False

        if st["touch_btn"] and not self.last_touch_btn:
            if st["touch_x"] > 960:
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            else:
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        elif not st["touch_btn"] and self.last_touch_btn:
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        self.last_touch_btn = st["touch_btn"]

    def worker_loop(self):
        # FIX 6: lee input rapido, pero escribe al HID solo si algo cambio
        last_heartbeat = time.time()
        read_failures = 0  # Contador para detectar desconexión física del mando

        while self.running_thread and self.hw.connected:
            st = self.hw.read_state()
            if st:
                read_failures = 0  # Reset al leer bien
                # NAVEGACIÓN POR CONTROL (cuadros de perfil): si un cuadro
                # está registrado, el control lo maneja sin clics (0
                # parpadeos): D-pad arriba/abajo mueve, Cross acepta,
                # Circle cancela. Solo flanco de pulsación (mantener no
                # repite). Los botones de navegación NO se reenvían al pad
                # virtual, para que aceptar no dispare una acción en el juego.
                # Toda la UI corre en el hilo principal vía after(0).
                nav = getattr(self, "_pad_nav", None)
                if nav is not None:
                    try:
                        _alive = nav["dialog"].winfo_exists()
                    except Exception:
                        _alive = False
                    if not _alive:
                        self._pad_nav = None
                        self._pad_nav_prev = None
                    else:
                        _prev = getattr(self, "_pad_nav_prev", None)
                        if _prev is None:
                            # Armar sin disparar: un botón ya mantenido al
                            # abrir el cuadro no debe aceptar de inmediato.
                            self._pad_nav_prev = {k: bool(st.get(k, False)) for k in ("up", "down", "cross", "circle")}
                        else:
                            _cur = {k: bool(st.get(k, False)) for k in ("up", "down", "cross", "circle")}
                            if _cur["down"] and not _prev["down"]:
                                try:
                                    self.after(0, lambda: nav["move"](1))
                                except Exception:
                                    pass
                            if _cur["up"] and not _prev["up"]:
                                try:
                                    self.after(0, lambda: nav["move"](-1))
                                except Exception:
                                    pass
                            if _cur["cross"] and not _prev["cross"]:
                                try:
                                    self.after(0, nav["accept"])
                                except Exception:
                                    pass
                            if _cur["circle"] and not _prev["circle"]:
                                try:
                                    self.after(0, nav["cancel"])
                                except Exception:
                                    pass
                            self._pad_nav_prev = _cur
                        # No reenviar navegación al juego.
                        for _k in ("up", "down", "left", "right", "cross", "circle"):
                            try:
                                st[_k] = False
                            except Exception:
                                pass
                # CAMBIO RÁPIDO de perfil dentro del juego, dos formas:
                # (1) mantener el modificador + flecha; (2) un dedo: tocar
                # el modificador y luego la flecha dentro de 3 segundos.
                # Modificador según emulación actual: X360 -> clic del pad;
                # DS4/Solo (NONE) -> Share. El modificador y la flecha se
                # tragan; un toque solo del modificador se reenvía al
                # vencer la ventana (el juego sigue viendo su Share/clic
                # normal, con hasta 3 s de retraso). Un solo cambio por
                # cada mantener o cada ventana. En Solo (sin mando virtual)
                # el juego lee el control físico directo y no se puede
                # tragar nada: el cambio aplica igual, pero el juego
                # también ve el Share y la flecha.
                try:
                    _qs_dirs = ("up", "down", "left", "right")
                    _qs_mod_key = "touch_btn" if self.emulation_type == "X360" else "share"
                    _qs_mod = bool(st.get(_qs_mod_key, False))
                    _qs_phys = {d: bool(st.get(d, False)) for d in _qs_dirs}
                    _qs_now = time.time()
                    # Set vigente: el del juego activo si lo personalizó,
                    # si no el ajuste general.
                    _qs_map = self._resolve_quick_switch_map(self._last_auto_game)
                    if _qs_mod:
                        # Mantener real: cancela ventana e inyección pendientes.
                        self._qs_window_until = 0.0
                        self._qs_inject = 0
                        if not self._qs_prev_mod:
                            self._qs_switched = False
                        self._qs_suppress_dpad = True
                        if not self._qs_switched:
                            for _d in _qs_dirs:
                                if _qs_phys[_d] and not self._qs_prev_dirs[_d]:
                                    _qs_target = _qs_map.get(_d)
                                    if _qs_target and _qs_target in self.profiles:
                                        self._qs_switched = True
                                        try:
                                            self.after(0, lambda _t=_qs_target: self._quick_switch_profile(_t))
                                        except Exception:
                                            pass
                                    break
                        # Tragar modificador + flechas mientras se mantiene.
                        st[_qs_mod_key] = False
                        for _d in _qs_dirs:
                            st[_d] = False
                    else:
                        if self._qs_prev_mod and not self._qs_switched:
                            # Soltó sin combo: abre la ventana de 3 s (un
                            # dedo: tocar modificador y luego la flecha).
                            # El toque pendiente solo se reenvía si la
                            # ventana vence sin flecha.
                            self._qs_window_until = _qs_now + QUICK_SWITCH_WINDOW
                            self._qs_suppress_dpad = True
                        self._qs_switched = False
                        if self._qs_window_until:
                            if _qs_now >= self._qs_window_until:
                                # Ventana vencida: reenviar el toque pendiente
                                # (solo Share en DS4 llega al juego; el clic
                                # del pad en X360 no se reenvía nunca, y en
                                # Solo el físico ya llegó directo).
                                self._qs_window_until = 0.0
                                if _qs_mod_key == "share" and self.emulation_type == "DS4":
                                    self._qs_inject = 2
                            else:
                                # Ventana abierta: una flecha = combo (mismo
                                # dedo), tragada; el toque queda pendiente.
                                for _d in _qs_dirs:
                                    if _qs_phys[_d] and not self._qs_prev_dirs[_d]:
                                        _qs_target = _qs_map.get(_d)
                                        if _qs_target and _qs_target in self.profiles:
                                            self._qs_window_until = 0.0
                                            try:
                                                self.after(0, lambda _t=_qs_target: self._quick_switch_profile(_t))
                                            except Exception:
                                                pass
                                        break
                                for _d in _qs_dirs:
                                    st[_d] = False
                        if not self._qs_window_until:
                            if not any(_qs_phys.values()):
                                self._qs_suppress_dpad = False
                            elif self._qs_suppress_dpad:
                                for _d in _qs_dirs:
                                    st[_d] = False
                    if self._qs_inject > 0:
                        st["share"] = True
                        self._qs_inject -= 1
                    self._qs_prev_mod = _qs_mod
                    self._qs_prev_dirs = _qs_phys
                except Exception:
                    pass
                # FIX 9: bateria (throttle: solo si cambio el texto)
                batt_txt = self._batt_text(st)
                if batt_txt != self._last_batt_txt:
                    self._last_batt_txt = batt_txt
                    try:
                        self.batt_label.configure(text=batt_txt)
                    except Exception:
                        pass
                dz_l = self.dz_l_slider.get()
                dz_r = self.dz_r_slider.get()
                curve = self.curve_slider.get()

                lx_f = self.process_axis(st["LX"], dz_l, curve, invert=False)
                ly_f = self.process_axis(st["LY"], dz_l, curve, invert=True)
                rx_f = self.process_axis(st["RX"], dz_r, curve, invert=False)
                ry_f = self.process_axis(st["RY"], dz_r, curve, invert=True)

                if self.touch_switch.get():
                    self.handle_touchpad_mouse(st, self.sens_slider.get())

                pad = self.virtual_pad

                if pad and self.emulation_type == "X360":
                    pad.reset()
                    if st["cross"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
                    if st["circle"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
                    if st["square"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                    if st["triangle"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
                    if st["L1"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
                    if st["R1"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
                    if st["L3"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
                    if st["R3"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)
                    if st["share"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)
                    if st["options"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
                    if st["ps"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE)
                    if st["up"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP)
                    if st["down"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN)
                    if st["left"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT)
                    if st["right"]:
                        pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT)

                    pad.left_trigger(value=st["L2"])
                    pad.right_trigger(value=st["R2"])
                    pad.left_joystick(x_value=int(lx_f * 32767), y_value=int(ly_f * 32767))
                    pad.right_joystick(x_value=int(rx_f * 32767), y_value=int(ry_f * 32767))
                    pad.update()

                elif pad and self.emulation_type == "DS4":
                    pad.reset()
                    if st["cross"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_CROSS)
                    if st["circle"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_CIRCLE)
                    if st["square"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_SQUARE)
                    if st["triangle"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_TRIANGLE)
                    if st["L1"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_LEFT)
                    if st["R1"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_RIGHT)
                    if st["share"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_SHARE)
                    if st["options"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_OPTIONS)
                    if st["L3"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_THUMB_LEFT)
                    if st["R3"]:
                        pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_THUMB_RIGHT)
                    if st["ps"]:
                        pad.press_special_button(vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_PS)
                    # Touchpad: clic pass-through al juego solo si el modo
                    # ratón está apagado (si está encendido, el touchpad
                    # mueve el cursor, comportamiento elegido por el usuario).
                    # En modo X360 no hay touchpad que pasar (el mando Xbox
                    # no tiene); la posición del dedo no la expone vgamepad
                    # (solo el clic), así que solo va el clic.
                    if st["touch_btn"] and not self.touch_switch.get():
                        pad.press_special_button(vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_TOUCHPAD)
                    # D-pad (hat): combinar las 4 direcciones (con diagonales).
                    _up, _down, _left, _right = st["up"], st["down"], st["left"], st["right"]
                    if _up and _right:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHEAST)
                    elif _right and _down:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHEAST)
                    elif _down and _left:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHWEST)
                    elif _left and _up:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHWEST)
                    elif _up:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTH)
                    elif _right:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST)
                    elif _down:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTH)
                    elif _left:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST)
                    else:
                        pad.directional_pad(vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NONE)

                    pad.left_trigger(value=st["L2"])
                    pad.right_trigger(value=st["R2"])
                    pad.left_joystick(x_value=int((lx_f + 1.0) * 127.5), y_value=int((-ly_f + 1.0) * 127.5))
                    pad.right_joystick(x_value=int((rx_f + 1.0) * 127.5), y_value=int((-ry_f + 1.0) * 127.5))
                    # Touchpad: pass-through de POSICIÓN vía reporte extendido
                    # DS4 (vgamepad no expone posición; solo el clic). Si algo
                    # falla, se cae al update() normal (sin posición, pero con
                    # todo el input intacto). Solo modo DS4; en X360 no hay
                    # touchpad que pasar.
                    _sent_ex = False
                    _vcom = getattr(self, "_vcom", None)
                    if _vcom is not False:
                        try:
                            if _vcom is None:
                                from vgamepad.win import vigem_commons as _vcom
                                self._vcom = _vcom
                            # c_byte es CON SIGNO: convertir bytes crudos
                            # (0-255) o ctypes rechaza todo valor >= 128.
                            _sb = lambda v: v - 256 if v >= 128 else v
                            _ex = _vcom.DS4_REPORT_EX()
                            _r = _ex.Report
                            _r.bThumbLX = pad.report.bThumbLX
                            _r.bThumbLY = pad.report.bThumbLY
                            _r.bThumbRX = pad.report.bThumbRX
                            _r.bThumbRY = pad.report.bThumbRY
                            _r.wButtons = pad.report.wButtons
                            _r.bSpecial = pad.report.bSpecial
                            _r.bTriggerL = pad.report.bTriggerL
                            _r.bTriggerR = pad.report.bTriggerR
                            _touching = bool(st["touch_active"]) and not self.touch_switch.get()
                            _was = getattr(self, "_ex_touch_prev_active", False)
                            if _touching and not _was:
                                self._ex_touch_id = (getattr(self, "_ex_touch_id", 0) + 1) & 0x7F
                                self._ex_down_t = time.time()
                                self._ex_traj = []
                                self._ex_traj_clicked = False
                            if _touching:
                                _rx = max(0, min(1919, int(st["touch_x"])))
                                # Reescala Y: el DualSense reporta 0-1079 y la
                                # superficie DS4 es 0-941. Sin reescala, la
                                # vertical salia ~15% caliente y el clamp en
                                # 943 dejaba pasar valores fuera de rango.
                                _ry = max(0, min(941, int(round(int(st["touch_y"]) * 941 / 1079))))
                                if not _was:
                                    self._ex_down_x, self._ex_down_y = _rx, _ry
                                # Sin zona muerta: congelar en el punto de
                                # apoyo y soltar de golpe crea los saltos que
                                # el juego leia como gestos.
                                _tx, _ty = _rx, _ry
                                self._ex_last_x, self._ex_last_y = _tx, _ty
                                if st.get("touch_btn"):
                                    self._ex_traj_clicked = True
                                _traj = getattr(self, "_ex_traj", None)
                                if _traj is not None and len(_traj) < 5000:
                                    _traj.append((int((time.time() - getattr(self, "_ex_down_t", time.time())) * 1000), _rx, _ry, _tx, _ty))
                            else:
                                _tx = getattr(self, "_ex_last_x", 0)
                                _ty = getattr(self, "_ex_last_y", 0)
                            # N=1 SIEMPRE: el hardware manda el estado up en
                            # cada reporte (ultimo ID, ultimas coords) hasta
                            # el proximo contacto. Un solo paquete up + N=0
                            # se lo puede perder el juego y fusiona el gesto
                            # siguiente con el anterior. Contador +1 por
                            # reporte emitido (mod 256); nunca se copia el
                            # contador del DualSense (byte 41, va al final).
                            _tid = getattr(self, "_ex_touch_id", 0) & 0x7F
                            self._ex_touch_counter = (getattr(self, "_ex_touch_counter", 0) + 1) & 0xFF
                            _cur = _r.sCurrentTouch
                            _cur.bPacketCounter = _sb(self._ex_touch_counter)
                            _cur.bIsUpTrackingNum1 = _sb(_tid | (0x00 if _touching else 0x80))
                            _cur.bTouchData1[0] = _sb(_tx & 0xFF)
                            _cur.bTouchData1[1] = _sb(((_tx >> 8) & 0x0F) | ((_ty & 0x0F) << 4))
                            _cur.bTouchData1[2] = _sb((_ty >> 4) & 0xFF)
                            _cur.bIsUpTrackingNum2 = _sb(0x80)
                            _cur.bTouchData2[0] = 0
                            _cur.bTouchData2[1] = 0
                            _cur.bTouchData2[2] = 0
                            _r.bTouchPacketsN = 1
                            # Previous: patron "sin uso" siempre (con N=1 no
                            # hay historia real; ceros serian un toque DOWN
                            # en (0,0) con ID 0).
                            for _p in (_r.sPreviousTouch[0], _r.sPreviousTouch[1]):
                                _p.bPacketCounter = 0
                                _p.bIsUpTrackingNum1 = _sb(0x80)
                                _p.bTouchData1[0] = 0
                                _p.bTouchData1[1] = 0
                                _p.bTouchData1[2] = 0
                                _p.bIsUpTrackingNum2 = _sb(0x80)
                                _p.bTouchData2[0] = 0
                                _p.bTouchData2[1] = 0
                                _p.bTouchData2[2] = 0
                            self._ex_touch_prev_active = _touching
                            pad.update_extended_report(_ex)
                            _sent_ex = True
                        except ImportError:
                            self._vcom = False
                        except Exception:
                            pass
                    if not _sent_ex:
                        pad.update()
                    # Log de diagnóstico (post-hoc, no toca el input): al
                    # soltar el dedo, escribe la trayectoria del gesto para
                    # ver qué recibió el juego (dirección real vs la que el
                    # juego interpretó).
                    if _was and not _touching:
                        try:
                            _traj = getattr(self, "_ex_traj", None) or []
                            _dx0 = getattr(self, "_ex_down_x", 0)
                            _dy0 = getattr(self, "_ex_down_y", 0)
                            _fx = getattr(self, "_ex_last_x", 0)
                            _fy = getattr(self, "_ex_last_y", 0)
                            _lines = [f"gesto: inicio=({_dx0},{_dy0}) fin=({_fx},{_fy}) dx={_fx - _dx0} dy={_fy - _dy0} frames={len(_traj)} clic={'SI' if getattr(self, '_ex_traj_clicked', False) else 'NO'} ex={'SI' if _sent_ex else 'NO'}"]
                            for _t, _ax, _ay, _bx, _by in _traj:
                                _lines.append(f"  t={_t} x={_ax} y={_ay} rep=({_bx},{_by})")
                            with open(os.path.join(BASE_DIR, "touch_log.txt"), "a", encoding="utf-8") as _f:
                                _f.write("\n".join(_lines) + "\n")
                        except Exception:
                            pass
                        self._ex_traj = []

                # FIX 6: solo escribe al control si config o rumble cambiaron
                ui_st = self.collect_ui_state()
                if not getattr(self, "_profile_active", True):
                    # Sin perfil activo: gatillos desactivados, rumble y luz vivos.
                    ui_st = dict(ui_st)
                    ui_st["l2_mode"] = "Desactivado"
                    ui_st["r2_mode"] = "Desactivado"
                sig = (ui_st["l2_mode"], ui_st["l2_start"], ui_st["l2_end"], ui_st["l2_force"],
                       ui_st["r2_mode"], ui_st["r2_start"], ui_st["r2_end"], ui_st["r2_force"],
                       tuple(ui_st["lightbar"]), self.rumble_small, self.rumble_large)
                now = time.time()
                heartbeat_due = (now - last_heartbeat) > 2.5 and self.heartbeat_enabled
                if sig != self._last_sig or heartbeat_due:
                    l2_cfg = {"mode": ui_st["l2_mode"], "start": ui_st["l2_start"], "end": ui_st["l2_end"],
                              "force": ui_st["l2_force"]}
                    r2_cfg = {"mode": ui_st["r2_mode"], "start": ui_st["r2_start"], "end": ui_st["r2_end"],
                              "force": ui_st["r2_force"]}
                    reason = "CAMBIO" if sig != self._last_sig else "HEARTBEAT"
                    self._debug_log_write(f"LOOP {reason} rumble=({self.rumble_small},{self.rumble_large})")
                    self.hw.send_packet(r2_cfg, l2_cfg, ui_st["lightbar"],
                                        self.rumble_small, self.rumble_large)
                    self._last_sig = sig
                    last_heartbeat = now
            else:
                # read_state() falló: posible desconexión física
                read_failures += 1
                if read_failures > 100:  # ~0.5s de fallos continuos
                    self.hw.connected = False
                    try:
                        self.after(0, self._on_device_lost)
                    except Exception:
                        pass
                    break

            time.sleep(0.005)


if __name__ == "__main__":
    app = DualSenseStudioApp()
    if "--tray" in sys.argv or "--minimized" in sys.argv:
        app.hide_to_tray()  # FIX 8: arranca minimizado (para el autostart)
    app.mainloop()
