import argparse
import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import ctypes
import cv2
import mss
import numpy as np
import psutil
import pyautogui
import uvicorn
import io
import time
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw
import threading
import queue

try:
    import dxcam
except ImportError:
    dxcam = None
import soundcard as sc

import pystray
import pyperclip
import tkinter as tk
from tkinter import ttk

# pywin32 — used only for talking to the unlock service over a named pipe.
try:
    import win32file
    import pywintypes
    UNLOCK_SERVICE_AVAILABLE = True
except ImportError:
    UNLOCK_SERVICE_AVAILABLE = False

active_connections = {
    "mjpeg": 0,
    "audio": 0,
    "control": 0
}


DEFAULT_TOKEN = "YOUR-TOKEN"
TOKEN = os.getenv("REMOTE_DESKTOP_TOKEN", DEFAULT_TOKEN)
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))
STREAM_FPS = 60

# Must match SERVICE_AUTH_TOKEN in unlock_service.py.
UNLOCK_PIPE_NAME = r"\\.\pipe\ConnectUnlockService"
UNLOCK_AUTH_TOKEN = "kirthi911-unlock"

# dxcam returns one process-wide camera for each device/output/backend tuple.
# Keep this camera alive for the agent lifetime so concurrent MJPEG clients only
# borrow it instead of attempting to start the same singleton repeatedly.
_dxcam_camera = None
_dxcam_camera_lock = threading.Lock()
_dxcam_initialization_failed = False
_dxcam_reader_thread = None
_dxcam_reader_lock = threading.Lock()
_dxcam_reader_stop_event = threading.Event()
_latest_frame = None
_latest_frame_lock = threading.Lock()


def get_shared_dxcam_camera():
    """Return the single started DXcam camera, or None to use the mss fallback."""
    global _dxcam_camera, _dxcam_initialization_failed

    if _dxcam_camera is not None:
        return _dxcam_camera

    with _dxcam_camera_lock:
        # Check again after acquiring the lock: another MJPEG client may have
        # created and started the camera while this caller was waiting.
        if _dxcam_camera is not None:
            return _dxcam_camera
        if _dxcam_initialization_failed:
            return None
        if dxcam is None:
            _dxcam_initialization_failed = True
            print("Warning: dxcam is unavailable; falling back to mss/GDI capture.")
            return None

        try:
            camera = dxcam.create(output_idx=0, output_color="BGR")
            if not camera.is_capturing:
                camera.start(target_fps=STREAM_FPS, video_mode=True)
            _dxcam_camera = camera
            print("Using shared DXGI desktop capture (dxcam) for MJPEG streaming.")
            return camera
        except Exception as error:
            _dxcam_initialization_failed = True
            print(
                "Warning: shared DXGI desktop capture could not start "
                f"({error}); falling back to mss/GDI capture."
            )
            return None


def _dxcam_reader_loop():
    """The only process thread allowed to consume DXcam's frame buffer."""
    global _latest_frame

    while not _dxcam_reader_stop_event.is_set():
        camera = get_shared_dxcam_camera()
        if camera is None:
            return

        try:
            # In dxcam 0.3.0 this blocks until the producer has buffered a
            # frame, so a sleep after a successful read would only add latency.
            frame = camera.get_latest_frame()
        except Exception as error:
            print(f"Shared DXGI frame reader error: {error}")
            time.sleep(0.1)
            continue

        if frame is not None:
            # get_latest_frame(copy=True) is the default, so this frame is no
            # longer backed by dxcam's mutable ring buffer.
            with _latest_frame_lock:
                _latest_frame = frame
        elif not _dxcam_reader_stop_event.is_set():
            # None means capture stopped or its buffer is unavailable. Yield so
            # an unexpected non-blocking implementation cannot spin a CPU core.
            time.sleep(0.01)


def start_shared_dxcam_reader() -> bool:
    """Lazily start the one reader that publishes frames for all clients."""
    global _dxcam_reader_thread

    if get_shared_dxcam_camera() is None:
        return False
    if _dxcam_reader_thread is not None and _dxcam_reader_thread.is_alive():
        return True

    with _dxcam_reader_lock:
        if _dxcam_reader_thread is not None and _dxcam_reader_thread.is_alive():
            return True
        _dxcam_reader_stop_event.clear()
        _dxcam_reader_thread = threading.Thread(
            target=_dxcam_reader_loop,
            name="DXcamFrameReader",
            daemon=True,
        )
        _dxcam_reader_thread.start()
    return True


def get_current_frame():
    """Return the latest completed DXcam frame for an individual MJPEG client."""
    with _latest_frame_lock:
        return _latest_frame


def stop_shared_dxcam_camera():
    """Release the process-wide DXcam resource during agent shutdown only."""
    global _dxcam_camera, _dxcam_reader_thread, _latest_frame

    _dxcam_reader_stop_event.set()

    with _dxcam_camera_lock:
        camera = _dxcam_camera
        _dxcam_camera = None
    if camera is not None:
        try:
            camera.stop()
        except Exception:
            pass
    with _dxcam_reader_lock:
        reader_thread = _dxcam_reader_thread
        _dxcam_reader_thread = None
    if reader_thread is not None and reader_thread is not threading.current_thread():
        reader_thread.join(timeout=1.0)
    with _latest_frame_lock:
        _latest_frame = None


# ---------------------------------------------------------------------------
# Lifecycle (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup: nothing needed yet ---
    yield
    # --- shutdown ---
    stop_shared_dxcam_camera()
    # (previously called ngrok.kill() here, but ngrok was never used/imported
    # in this file — removed as dead code)


app = FastAPI(title="Remote Desktop Laptop Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

pyautogui.FAILSAFE = True


class MouseMove(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class MouseClick(BaseModel):
    x: Optional[int] = Field(default=None, ge=0)
    y: Optional[int] = Field(default=None, ge=0)
    button: str = "left"
    clicks: int = Field(default=1, ge=1, le=2)


class KeyboardInput(BaseModel):
    key: Optional[str] = None
    text: Optional[str] = None

class KeyboardHotkey(BaseModel):
    keys: List[str]

class MouseScroll(BaseModel):
    clicks: int


SPECIAL_KEYS = {
    "win": "win",
    "windows": "win",
    "esc": "esc",
    "escape": "esc",
    "enter": "enter",
    "backspace": "backspace",
}


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def get_primary_monitor() -> Dict[str, int]:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        return {"width": monitor["width"], "height": monitor["height"]}


def process_snapshot() -> List[Dict[str, Any]]:
    processes: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
        try:
            info = proc.info
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "",
                    "user": info.get("username") or "",
                    "cpu": round(float(info.get("cpu_percent") or 0), 1),
                    "ram": round(float(info.get("memory_percent") or 0), 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    processes.sort(key=lambda item: (item["cpu"], item["ram"]), reverse=True)
    return processes[:50]


# ---------------------------------------------------------------------------
# Lock-screen detection + unlock
# ---------------------------------------------------------------------------

def is_workstation_locked() -> bool:
    """True when the interactive desktop can't be opened — i.e. the
    Winlogon secure desktop (lock screen) is showing instead."""
    hDesktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0)
    if not hDesktop:
        return True
    ctypes.windll.user32.CloseDesktop(hDesktop)
    return False


def build_placeholder_frame(width: int = 1280, height: int = 720) -> bytes:
    img = Image.new("RGB", (width, height), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    text = "Locked — tap Unlock in the app"
    try:
        bbox = draw.textbbox((0, 0), text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = (len(text) * 8, 16)
    draw.text(((width - tw) // 2, (height - th) // 2), text, fill=(226, 232, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


PLACEHOLDER_FRAME = build_placeholder_frame()


def request_unlock_from_service() -> bool:
    """Ask ConnectUnlockService (a separate Windows Service) to type the
    stored password on the Winlogon desktop. Returns False if the
    service isn't installed/running or refuses the request."""
    if not UNLOCK_SERVICE_AVAILABLE:
        return False
    try:
        handle = win32file.CreateFile(
            UNLOCK_PIPE_NAME,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
        win32file.WriteFile(handle, f"{UNLOCK_AUTH_TOKEN}:unlock".encode("utf-8"))
        _, resp = win32file.ReadFile(handle, 64)
        win32file.CloseHandle(handle)
        return resp.strip() == b"OK"
    except pywintypes.error:
        return False


# ---------------------------------------------------------------------------
# Informational endpoints
# ---------------------------------------------------------------------------

@app.get("/status", dependencies=[Depends(require_token)])
def status() -> Dict[str, Any]:
    return {
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": psutil.virtual_memory().percent,
        "processes": process_snapshot(),
        "locked": is_workstation_locked(),
    }


@app.get("/screen")
def screen_info(token: str = Query(default="")) -> Dict[str, Any]:
    """Return the primary monitor resolution. Auth via ?token= query param."""
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    if is_workstation_locked():
        return {"width": 1280, "height": 720, "locked": True}
    info = get_primary_monitor()
    info["locked"] = False
    return info


# ---------------------------------------------------------------------------
# Unlock control endpoint
# ---------------------------------------------------------------------------

@app.post("/control/unlock", dependencies=[Depends(require_token)])
def unlock_workstation() -> Dict[str, Any]:
    if not is_workstation_locked():
        return {"ok": True, "already_unlocked": True}
    ok = request_unlock_from_service()
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="Unlock service unavailable or refused the request. "
                   "Is ConnectUnlockService installed and running?",
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# MJPEG stream endpoint
# ---------------------------------------------------------------------------

class CaptureManager:
    def __init__(self):
        self.queue = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.last_tick = time.time()
        self.thread = None
        self.watchdog_thread = None
        self.camera = None
        self.use_dxcam = True

    def start(self):
        self.stop_event.clear()
        self.last_tick = time.time()
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def stop(self):
        self.stop_event.set()
        self._stop_camera()
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=1.0)

    def _start_dxcam(self):
        """Borrow the process-wide DXcam camera, or use the mss fallback."""
        if not self.use_dxcam:
            return None

        self.camera = get_shared_dxcam_camera()
        if self.camera is not None and not start_shared_dxcam_reader():
            self.camera = None
        return self.camera

    def _stop_camera(self, camera=None):
        # CaptureManager only borrows the shared camera. A disconnect or
        # watchdog restart must never stop it while another viewer uses it.
        if camera is None or self.camera is camera:
            self.camera = None

    def _capture_loop(self):
        camera = self._start_dxcam()
        sct = None
        monitor = None
        if camera is None:
            sct = mss.mss()
            monitor = sct.monitors[1]

        try:
            last_hash = None
            last_frame_data = None
            last_frame_time = 0.0
            was_locked = False
            last_placeholder_push = 0.0

            while not self.stop_event.is_set():
                start_time = time.time()
                self.last_tick = start_time

                if is_workstation_locked():
                    was_locked = True
                    # Push the placeholder about once a second so the
                    # MJPEG stream stays alive without hammering the CPU.
                    if start_time - last_placeholder_push >= 1.0:
                        if self.queue.full():
                            try:
                                self.queue.get_nowait()
                            except queue.Empty:
                                pass
                        self.queue.put(PLACEHOLDER_FRAME)
                        last_placeholder_push = start_time
                    time.sleep(0.2)
                    continue

                if was_locked:
                    # Just unlocked — reset capture state so we don't
                    # compare against a stale hash from before locking.
                    was_locked = False
                    last_hash = None

                try:
                    if camera is not None:
                        frame = get_current_frame()
                        if frame is None:
                            # The single DXcam reader has not published a frame yet.
                            # Retain the existing per-client keepalive behavior.
                            if (
                                last_frame_data is not None
                                and (start_time - last_frame_time) >= 1.0
                            ):
                                if self.queue.full():
                                    try:
                                        self.queue.get_nowait()
                                    except queue.Empty:
                                        pass
                                self.queue.put(last_frame_data)
                                last_frame_time = start_time
                            time.sleep(0.005)
                            continue
                    else:
                        sct_img = sct.grab(monitor)
                        # mss produces BGRA; JPEG encoding only needs BGR.
                        frame = np.asarray(sct_img)[:, :, :3]
                except Exception as error:
                    print(f"Screen capture error: {error}")
                    if camera is not None:
                        self._stop_camera(camera)
                        camera = None
                        self.use_dxcam = False
                        print("Warning: DXGI capture failed; falling back to mss/GDI capture.")
                    else:
                        try:
                            sct.close()
                        except Exception:
                            pass
                    time.sleep(0.1)
                    if camera is None:
                        sct = mss.mss()
                        monitor = sct.monitors[1]
                    continue

                current_hash = hash(frame[::50, ::50].tobytes())
                
                if current_hash != last_hash:
                    success, encoded = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90]
                    )
                    if not success:
                        print("JPEG encoding failed; skipping frame.")
                        continue
                    frame_data = encoded.tobytes()
                    
                    if self.queue.full():
                        try:
                            self.queue.get_nowait()
                        except queue.Empty:
                            pass
                    self.queue.put(frame_data)
                    last_hash = current_hash
                    last_frame_data = frame_data
                    last_frame_time = start_time
                elif last_frame_data is not None and (start_time - last_frame_time) >= 1.0:
                    # Keepalive: resend last frame so the MJPEG decoder never stalls
                    if self.queue.full():
                        try:
                            self.queue.get_nowait()
                        except queue.Empty:
                            pass
                    self.queue.put(last_frame_data)
                    last_frame_time = start_time
                    
                elapsed = time.time() - start_time
                sleep_time = max(0, (1.0 / STREAM_FPS) - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        finally:
            self._stop_camera(camera)
            if sct is not None:
                try:
                    sct.close()
                except Exception:
                    pass

    def _watchdog_loop(self):
        while not self.stop_event.is_set():
            time.sleep(1.0)
            if time.time() - self.last_tick > 5.0 and not self.stop_event.is_set():
                print("Watchdog triggered: capture thread frozen for 5 seconds. Restarting...")
                self.last_tick = time.time()
                self.thread = threading.Thread(target=self._capture_loop, daemon=True)
                self.thread.start()

def generate_mjpeg_stream(token: str):
    active_connections["mjpeg"] += 1
    manager = CaptureManager()
    manager.start()
    try:
        while True:
            try:
                frame_data = manager.queue.get(timeout=0.1)
                header = (f"--frame\r\n"
                          f"Content-Type: image/jpeg\r\n"
                          f"Content-Length: {len(frame_data)}\r\n\r\n").encode("utf-8")
                yield header + frame_data + b"\r\n"
            except queue.Empty:
                pass
    finally:
        manager.stop()
        active_connections["mjpeg"] -= 1

@app.get("/mjpeg")
def mjpeg_stream(token: str = Query(default="")):
    """Stream MJPEG video."""
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")

    return StreamingResponse(
        generate_mjpeg_stream(token),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

# ---------------------------------------------------------------------------
# Audio stream endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws/audio")
async def audio_stream(websocket: WebSocket, token: str = Query(default="")):
    """Stream real-time system audio."""
    if token != TOKEN:
        await websocket.close(code=1008, reason="Invalid token")
        return

    await websocket.accept()
    print("Audio WebSocket client connected.")

    active_connections["audio"] += 1
    process = subprocess.Popen(
        [sys.executable, "audio_capture.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).parent)
    )

    try:
        while True:
            chunk = await asyncio.to_thread(process.stdout.read, 2048)
            if not chunk:
                break
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        print("Audio client disconnected.")
    except Exception as e:
        print(f"Audio stream error: {e}")
    finally:
        active_connections["audio"] -= 1
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()




# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------

@app.post("/control/move", dependencies=[Depends(require_token)])
def move_mouse(payload: MouseMove) -> Dict[str, bool]:
    pyautogui.moveTo(payload.x, payload.y, duration=0)
    return {"ok": True}


@app.post("/control/click", dependencies=[Depends(require_token)])
def click_mouse(payload: MouseClick) -> Dict[str, bool]:
    if payload.x is not None and payload.y is not None:
        pyautogui.moveTo(payload.x, payload.y, duration=0)
    pyautogui.click(button=payload.button, clicks=payload.clicks, interval=0.08)
    return {"ok": True}


@app.post("/control/key", dependencies=[Depends(require_token)])
def keyboard(payload: KeyboardInput) -> Dict[str, bool]:
    if payload.text:
        pyautogui.write(payload.text, interval=0)
    elif payload.key:
        normalized = payload.key.lower().strip()
        pyautogui.press(SPECIAL_KEYS.get(normalized, normalized))
    else:
        raise HTTPException(status_code=400, detail="Provide key or text")
    return {"ok": True}


@app.post("/control/hotkey", dependencies=[Depends(require_token)])
def keyboard_hotkey(payload: KeyboardHotkey) -> Dict[str, bool]:
    if not payload.keys:
        raise HTTPException(status_code=400, detail="Provide keys for hotkey")
    pyautogui.hotkey(*payload.keys)
    return {"ok": True}


@app.post("/control/scroll", dependencies=[Depends(require_token)])
def scroll_mouse(payload: MouseScroll) -> Dict[str, bool]:
    pyautogui.scroll(payload.clicks)
    return {"ok": True}


@app.post("/control/screenshot", dependencies=[Depends(require_token)])
def save_screenshot() -> Dict[str, Any]:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"remote-screenshot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    path = SCREENSHOT_DIR / filename

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        image.save(path, format="PNG")

    return {"ok": True, "filename": filename, "path": str(path.resolve())}


# ---------------------------------------------------------------------------
# System Tray GUI and Server Management
# ---------------------------------------------------------------------------

server_thread = None
server_instance = None
tray_icon = None
listen_port = 8000

def get_tailscale_ip():
    try:
        result = subprocess.run(
            [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"],
            capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout.strip()
    except Exception:
        return "127.0.0.1"

def start_server_thread():
    global server_thread, server_instance
    if server_instance is not None:
        return
    config = uvicorn.Config(app, host="0.0.0.0", port=listen_port, log_level="error")
    server_instance = uvicorn.Server(config)
    server_thread = threading.Thread(target=server_instance.run, daemon=True)
    server_thread.start()

def stop_server_thread():
    global server_instance
    if server_instance is not None:
        server_instance.should_exit = True
        server_instance = None

def on_start_agent(icon, item):
    start_server_thread()

def on_stop_agent(icon, item):
    stop_server_thread()

def on_copy_url(icon, item):
    ip = get_tailscale_ip()
    url = f"http://{ip}:{listen_port}"
    pyperclip.copy(url)

def show_status_window():
    root = tk.Tk()
    root.title("Agent Status")
    root.geometry("250x150")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    
    ttk.Label(root, text="System Status", font=("Segoe UI", 12, "bold")).pack(pady=10)
    
    cpu_var = tk.StringVar(value=f"CPU: {psutil.cpu_percent()}%")
    ram_var = tk.StringVar(value=f"RAM: {psutil.virtual_memory().percent}%")
    conn_var = tk.StringVar(value=f"WebSockets: MJPEG({active_connections['mjpeg']}) Audio({active_connections['audio']})")
    
    ttk.Label(root, textvariable=cpu_var).pack()
    ttk.Label(root, textvariable=ram_var).pack()
    ttk.Label(root, textvariable=conn_var).pack(pady=5)
    
    def update_stats():
        cpu_var.set(f"CPU: {psutil.cpu_percent()}%")
        ram_var.set(f"RAM: {psutil.virtual_memory().percent}%")
        conn_var.set(f"WebSockets: MJPEG({active_connections['mjpeg']}) Audio({active_connections['audio']})")
        root.after(1000, update_stats)
        
    update_stats()
    root.mainloop()

def on_show_status(icon, item):
    threading.Thread(target=show_status_window, daemon=True).start()

def on_exit(icon, item):
    stop_server_thread()
    icon.stop()

def create_tray_image():
    image = Image.new('RGB', (64, 64), color=(255, 255, 255))
    dc = ImageDraw.Draw(image)
    dc.ellipse((16, 16, 48, 48), fill='#2563eb')
    return image

def main() -> None:
    parser = argparse.ArgumentParser(description="Remote desktop laptop agent")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    
    global listen_port
    listen_port = args.port

    start_server_thread()

    menu = pystray.Menu(
        pystray.MenuItem("Show Status", on_show_status),
        pystray.MenuItem("Copy Tailscale URL", on_copy_url),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start Agent", on_start_agent),
        pystray.MenuItem("Stop Agent", on_stop_agent),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit)
    )

    global tray_icon
    tray_icon = pystray.Icon("RemoteDesktopAgent", create_tray_image(), "Connect Agent", menu)
    
    def notify_ready():
        time.sleep(1.0)
        tray_icon.notify("Remote Desktop Agent is running", "Connect Agent")

    threading.Thread(target=notify_ready, daemon=True).start()
    
    tray_icon.run()

if __name__ == "__main__":
    main()
