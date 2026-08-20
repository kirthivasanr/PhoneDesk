"""
unlock_helper.py
=================
Launched by unlock_service.py directly into the Winlogon secure desktop,
running as SYSTEM. Decrypts the stored password (DPAPI, machine scope)
and types it into the currently-focused password field, then presses
Enter.

This file must be compiled to unlock_helper.exe with PyInstaller and
placed next to unlock_service.py:

    pip install pyinstaller pywin32
    pyinstaller --onefile --noconsole unlock_helper.py
    (copy dist/unlock_helper.exe next to unlock_service.py)

Never runs standalone as a normal user — it relies on already being
placed on the Winlogon desktop by the service via lpDesktop.
"""

import ctypes
import logging
import os
import sys
import time
import traceback

import win32crypt

if getattr(sys, "frozen", False):
    # Running as a PyInstaller --onefile exe: __file__ would resolve to
    # the temp extraction folder (C:\Windows\Temp\_MEI...), not where
    # the exe actually lives. Use the exe's own path instead.
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PASSWORD_FILE = os.path.join(BASE_DIR, "unlock_secret.bin")
LOG_FILE = os.path.join(BASE_DIR, "unlock_helper.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    _anonymous_ = ("_i",)
    _fields_ = [("type", ctypes.c_ulong), ("_i", _I)]


def send_unicode_char(ch: str) -> None:
    down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None))
    up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def send_digit(d: str) -> None:
    """Send a digit 0-9 as a real virtual-key press rather than Unicode
    injection — PIN credential providers can filter/ignore synthetic
    Unicode input while still accepting real VK-based keystrokes."""
    vk = 0x30 + int(d)  # VK_0..VK_9 are 0x30-0x39, matching ASCII digits
    send_key(vk)


def send_key(vk_code: int) -> None:
    down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk_code, 0, 0, 0, None))
    up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk_code, 0, KEYEVENTF_KEYUP, 0, None))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def send_enter() -> None:
    down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_RETURN, 0, 0, 0, None))
    up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def load_password() -> str:
    with open(PASSWORD_FILE, "rb") as f:
        blob = f.read()
    # CryptUnprotectData returns (description, data) — machine-scope blob,
    # so any SYSTEM process on this machine can decrypt it.
    _, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data.decode("utf-8")


DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_ACCESS = DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP


def switch_to_desktop(name: str) -> bool:
    """Open the named desktop within the interactive window station and
    associate the current thread with it. Returns False (and logs) if
    it fails, rather than raising, so callers can decide how to proceed."""
    hdesk = ctypes.windll.user32.OpenDesktopW(name, 0, False, DESKTOP_ACCESS)
    if not hdesk:
        err = ctypes.GetLastError()
        logging.error("OpenDesktopW('%s') failed, error=%d", name, err)
        return False
    ok = ctypes.windll.user32.SetThreadDesktop(hdesk)
    if not ok:
        err = ctypes.GetLastError()
        logging.error("SetThreadDesktop('%s') failed, error=%d", name, err)
        return False
    logging.info("Switched thread to desktop '%s'", name)
    return True


def get_current_desktop_name() -> str:
    """For diagnostics: confirm which desktop this thread is actually
    associated with (should be 'Winlogon' if launched correctly)."""
    hdesk = ctypes.windll.user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
    buf = ctypes.create_unicode_buffer(256)
    needed = ctypes.c_ulong(0)
    ctypes.windll.user32.GetUserObjectInformationW(
        hdesk, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)  # 2 = UOI_NAME
    )
    return buf.value


def main() -> None:
    logging.info("=== unlock_helper started ===")
    try:
        logging.info("Running as frozen exe: %s", getattr(sys, "frozen", False))
        logging.info("BASE_DIR: %s", BASE_DIR)
        logging.info("Current desktop at launch: %s", get_current_desktop_name())

        if not os.path.exists(PASSWORD_FILE):
            logging.error("Password file not found at %s", PASSWORD_FILE)
            return

        # The wallpaper/clock screen shown right after Win+L runs on the
        # Default desktop, NOT the secure Winlogon desktop — Winlogon only
        # becomes the visible/active desktop once that wallpaper is
        # dismissed. On this machine, dismissal specifically requires
        # Enter (not just any key), matching manual behavior — switch
        # there explicitly and send Enter.
        if switch_to_desktop("Default"):
            time.sleep(0.3)
            logging.info("Sending Enter to dismiss wallpaper on Default desktop")
            send_key(VK_RETURN)
            time.sleep(1.5)
        else:
            logging.warning("Could not switch to Default desktop, proceeding anyway")

        # Now switch to the (now-active) Winlogon secure desktop to type
        # the PIN/password into the credential UI.
        if not switch_to_desktop("Winlogon"):
            logging.error("Could not switch to Winlogon desktop, aborting")
            return
        time.sleep(0.3)
        logging.info("Current desktop before typing: %s", get_current_desktop_name())

        password = load_password()
        logging.info("Password decrypted successfully, length=%d", len(password))

        for ch in password:
            if ch.isdigit():
                send_digit(ch)
            else:
                send_unicode_char(ch)
            time.sleep(0.05)
        send_enter()
        logging.info("Finished sending keystrokes + Enter")
    except Exception:
        logging.error("unlock_helper crashed:\n%s", traceback.format_exc())
    logging.info("=== unlock_helper exiting ===")


if __name__ == "__main__":
    main()