"""
ConnectUnlockService
=====================
A Windows Service (runs as LocalSystem) that listens on a local named
pipe for an "unlock" command from the Connect agent, and — when asked —
launches unlock_helper.exe *inside the active interactive session's
Winlogon (lock screen) desktop* to type the stored password.

Why a service at all: a normal user-mode process (your agent.py) can
never open the Winlogon desktop — that access is only granted to SYSTEM
processes running in the interactive session. A LocalSystem service
itself runs in Session 0 (isolated from your desktop), so it can't
interact with Winlogon directly either. The trick both RDP hosts and
tools like TeamViewer use: duplicate the service's own SYSTEM token,
re-point that token at the currently active interactive session, and
use CreateProcessAsUser to launch a small helper *into* that session
with lpDesktop="winsta0\\winlogon" — that helper process is now SYSTEM,
running on the secure desktop, and can call SendInput there.

Install (as Administrator, in an elevated cmd/PowerShell):
    pip install pywin32
    python unlock_service.py install
    python unlock_service.py --startup auto install   (to auto-start at boot)
    python unlock_service.py start

Uninstall:
    python unlock_service.py stop
    python unlock_service.py remove

Then run setup_password.py once (also as Administrator) to store your
Windows password encrypted via DPAPI before this will do anything useful.

IMPORTANT: unlock_helper.py must be compiled to unlock_helper.exe
(see build instructions in README.md) and placed in the same folder
as this script before the service will work end-to-end.
"""

import os
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil
import win32security
import win32process
import win32ts
import win32con
import win32api
import win32file
import win32pipe
import pywintypes

PIPE_NAME = r"\\.\pipe\ConnectUnlockService"


def _pipe_security_attributes():
    """By default, a named pipe created by a SYSTEM service isn't
    connectable by a normal-user process (Access is denied). Explicitly
    grant Everyone full control on this pipe instance — it's only
    reachable locally on this machine (named pipes aren't exposed over
    the network unless addressed as \\\\server\\pipe\\..., which we never
    do), so this doesn't widen remote exposure."""
    sddl = "D:(A;;GA;;;WD)"  # Everyone: Generic All
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1
    )
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa

# Must match the AUTH token used in agent.py's request_unlock().
# Change this to your own secret before deploying — treat it like a
# password, since anyone who can reach this pipe and knows it can
# trigger an unlock.
SERVICE_AUTH_TOKEN = "kirthi911-unlock"


def get_active_session_id() -> int:
    return win32ts.WTSGetActiveConsoleSessionId()


def launch_helper_in_session(session_id: int, helper_path: str) -> None:
    """Duplicate our SYSTEM token into the target session's Winlogon
    desktop and launch the helper exe there."""
    proc_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_DUPLICATE | win32con.TOKEN_ASSIGN_PRIMARY | win32con.TOKEN_QUERY,
    )

    new_token = win32security.DuplicateTokenEx(
        proc_token,
        win32security.SecurityImpersonation,
        win32con.TOKEN_ALL_ACCESS,
        win32security.TokenPrimary,
        None,
    )

    # Retarget the duplicated token to the currently active interactive
    # session, so CreateProcessAsUser launches into the right desktop.
    win32security.SetTokenInformation(new_token, win32security.TokenSessionId, session_id)

    startup = win32process.STARTUPINFO()
    startup.lpDesktop = "winsta0\\winlogon"  # target the secure desktop specifically

    win32process.CreateProcessAsUser(
        new_token,
        helper_path,
        None,
        None,
        None,
        False,
        win32con.CREATE_NO_WINDOW,
        None,
        None,
        startup,
    )


class UnlockService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ConnectUnlockService"
    _svc_display_name_ = "Connect Remote Unlock Service"
    _svc_description_ = "Lets the Connect remote-desktop agent unlock the Windows lock screen."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.running = False
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        threading.Thread(target=self.pipe_server_loop, daemon=True).start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

    def pipe_server_loop(self):
        while self.running:
            try:
                pipe = win32pipe.CreateNamedPipe(
                    PIPE_NAME,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    1, 65536, 65536, 0, _pipe_security_attributes(),
                )
                win32pipe.ConnectNamedPipe(pipe, None)
                _, data = win32file.ReadFile(pipe, 4096)
                message = data.decode("utf-8", errors="ignore").strip()

                if message == f"{SERVICE_AUTH_TOKEN}:unlock":
                    ok = self.handle_unlock()
                    win32file.WriteFile(pipe, b"OK" if ok else b"FAIL")
                else:
                    win32file.WriteFile(pipe, b"DENIED")

                win32file.FlushFileBuffers(pipe)
                win32pipe.DisconnectNamedPipe(pipe)
                win32file.CloseHandle(pipe)
            except pywintypes.error:
                continue

    def handle_unlock(self) -> bool:
        try:
            session_id = get_active_session_id()
            if session_id in (0xFFFFFFFF, None):
                servicemanager.LogErrorMsg("Unlock requested but no active interactive session found.")
                return False
            base_dir = os.path.dirname(os.path.abspath(__file__))
            helper_path = os.path.join(base_dir, "unlock_helper.exe")
            if not os.path.exists(helper_path):
                servicemanager.LogErrorMsg(f"unlock_helper.exe not found at {helper_path}")
                return False
            launch_helper_in_session(session_id, helper_path)
            return True
        except Exception as e:
            servicemanager.LogErrorMsg(f"Unlock failed: {e}")
            return False


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(UnlockService)
