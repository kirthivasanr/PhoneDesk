import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import mss
import psutil
import pyautogui
import uvicorn
import io
import time
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFilter
import threading
import queue
import soundcard as sc
import numpy as np

import pystray
import pyperclip
import tkinter as tk
from tkinter import ttk

active_connections = {
    "mjpeg": 0,
    "audio": 0,
    "control": 0
}


DEFAULT_TOKEN = "kirthi911"
TOKEN = os.getenv("REMOTE_DESKTOP_TOKEN", DEFAULT_TOKEN)
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))

app = FastAPI(title="Remote Desktop Laptop Agent")
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
# Lifecycle events
# ---------------------------------------------------------------------------

@app.on_event("shutdown")
def on_shutdown() -> None:
    try:
        ngrok.kill()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Informational endpoints
# ---------------------------------------------------------------------------

@app.get("/status", dependencies=[Depends(require_token)])
def status() -> Dict[str, Any]:
    return {
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": psutil.virtual_memory().percent,
        "processes": process_snapshot(),
    }


@app.get("/screen")
def screen_info(token: str = Query(default="")) -> Dict[str, Any]:
    """Return the primary monitor resolution. Auth via ?token= query param."""
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return get_primary_monitor()


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

    def start(self):
        self.stop_event.clear()
        self.last_tick = time.time()
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=1.0)

    def _capture_loop(self):
        sct = mss.mss()
        try:
            monitor = sct.monitors[1]
            last_hash = None
            last_mouse_pos = pyautogui.position()
            last_mouse_move_time = 0.0
            last_frame_data = None
            last_frame_time = 0.0
            
            while not self.stop_event.is_set():
                start_time = time.time()
                self.last_tick = start_time
                
                try:
                    sct_img = sct.grab(monitor)
                except Exception as e:
                    print(f"Screen capture error: {e}")
                    try:
                        sct.close()
                    except Exception:
                        pass
                    time.sleep(0.1)
                    sct = mss.mss()
                    monitor = sct.monitors[1]
                    continue
                    
                current_mouse_pos = pyautogui.position()
                if current_mouse_pos != last_mouse_pos:
                    last_mouse_move_time = start_time
                    last_mouse_pos = current_mouse_pos
                    
                current_hash = hash(sct_img.bgra[::1000])
                
                if current_hash != last_hash:
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    
                    if start_time - last_mouse_move_time < 1.0:
                        img = img.resize((1280, 720), Image.Resampling.BILINEAR)
                        
                    img = img.filter(ImageFilter.SHARPEN)
                    
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=97, optimize=False, subsampling=0)
                    frame_data = buf.getvalue()
                    
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
                sleep_time = max(0, (1.0 / 30.0) - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        finally:
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
