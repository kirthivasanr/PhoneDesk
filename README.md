# PhoneDesk  — Remote Desktop from Your Phone

Control and monitor your Windows laptop from anywhere using your Android phone. Built with a Python FastAPI agent on the laptop and a React Native app on mobile.

---

## What It Does

- Live screen streaming from your laptop to your phone
- Touch-to-click and drag controls
- Keyboard input from your phone
- System status monitoring (CPU, RAM, running processes)
- Screenshot capture
- Works on local WiFi and remotely via Tailscale or Cloudflare Tunnel
- Runs automatically at Windows startup via system tray

---

## Architecture

```
Phone App (React Native)
        ↕
  Tailscale / Local IP
        ↕
Python Agent (FastAPI)
        ↕
  Screen Capture (mss)
  JPEG Encoding (Pillow)
  Input Control (pyautogui)
```

### Streaming Approach

The agent captures the screen using `mss`, encodes each frame as JPEG using Pillow with `subsampling=0` for maximum sharpness, and streams it as **MJPEG (multipart/x-mixed-replace)** over HTTP. The mobile app renders the stream inside a WebView using an HTML `<img>` tag. Only changed frames are sent using a hash check to reduce bandwidth.

### Remote Access

For remote access outside your home network, **Tailscale** is used. It creates a permanent peer-to-peer VPN between your phone and laptop with a fixed IP that never changes. No relay servers, no random URLs.

---

## Tech Stack

### Laptop Agent
| Component | Technology |
|---|---|
| API Server | FastAPI + Uvicorn |
| Screen Capture | mss |
| Image Encoding | Pillow |
| Input Control | pyautogui |
| System Stats | psutil |
| System Tray | pystray |
| Audio Streaming | sounddevice |
| Python Version | 3.13 |

### Mobile App
| Component | Technology |
|---|---|
| Framework | React Native (Expo) |
| Stream Rendering | WebView + MJPEG img tag |
| Touch Controls | PanResponder |
| Build Tool | Android Studio + Gradle |
| Min Android | Android 8.0 (API 26) |

### Infrastructure
| Purpose | Tool |
|---|---|
| Remote Access | Tailscale (free) |
| Backup Tunnel | Cloudflare Tunnel (free) |
| Screen Encoding | MJPEG via Pillow |
| Hardware | NVIDIA GTX 1650 (NVENC available) |

---

## Project Structure

```
Connect/
├── agent/                        # Python laptop agent
│   ├── agent.py                  # Main FastAPI server + tray app
│   ├── start_agent.bat           # Windows startup batch file
│   ├── requirements.txt          # Python dependencies
│   ├── screenshots/              # Saved screenshots
│   └── .venv/                    # Python virtual environment
│
└── mobile/                       # React Native mobile app
    ├── App.tsx                   # Main app component
    ├── app.json                  # Expo configuration
    ├── package.json              # Node dependencies
    ├── assets/                   # Icons and splash screen
    └── android/                  # Native Android build files
```

---

## Prerequisites

### Laptop
- Windows 10/11
- Python 3.13
- FFmpeg installed and in PATH
- NVIDIA GPU (optional, for NVENC hardware encoding)
- Tailscale installed

### Mobile
- Android 8.0 or higher
- Tailscale app installed
- Same Tailscale account as laptop

### Development (to build the app)
- Node.js 18+
- Android Studio
- Android SDK
- Java JDK (bundled with Android Studio)

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/connect
cd connect
```

### 2. Set Up the Python Agent

```bash
cd agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Install FFmpeg

Download from https://www.gyan.dev/ffmpeg/builds/ — get `ffmpeg-release-essentials.zip`. Extract and add the `bin` folder to your system PATH.

Verify:
```bash
ffmpeg -version
```

### 4. Configure the Token

In `agent.py` find and set your token:
```python
DEFAULT_TOKEN = "your-secret-token-here"
```

### 5. Set Up the Mobile App

```bash
cd mobile
npm install
```

### 6. Build the Android APK

Make sure your phone is connected via USB with USB Debugging enabled, then:

```bash
npx expo run:android --variant release
```

The APK will be at:
```
mobile/android/app/build/outputs/apk/release/app-release.apk
```

Install it on your phone directly.

---

## Running the Agent

### Manual Start

```bash
cd agent
.venv\Scripts\python.exe agent.py
```

The system tray icon will appear. Right-click for options.

### Auto Start at Boot

The agent is configured to start automatically at Windows login via Task Scheduler. To set it up:

```cmd
schtasks /create /tn "RemoteDesktopAgent" /tr "C:\path\to\Connect\agent\start_agent.bat" /sc onlogon /ru YOUR_USERNAME /f
```

To test without restarting:
```cmd
schtasks /run /tn "RemoteDesktopAgent"
```

---

## System Tray Menu

Right-click the tray icon for these options:

| Option | Description |
|---|---|
| Show Status | Opens status window with CPU, RAM, connections |
| Copy Tailscale URL | Copies your permanent remote URL to clipboard |
| Start Agent | Starts the FastAPI server |
| Stop Agent | Stops the FastAPI server |
| Exit | Closes everything |

---

## Connecting from Your Phone

### Same WiFi (Local)
Enter in the app:
```
http://192.168.x.x:8000
```

### Remote (Tailscale)
1. Open Tailscale on your phone — make sure it's connected
2. Open the Connect app
3. Enter your laptop's Tailscale IP:
```
http://100.x.x.x:8000
```
4. Enter your token and tap Connect

The Tailscale IP never changes so you can save it permanently in the app.

### Remote (Cloudflare Tunnel — backup)
Run in a separate terminal:
```cmd
cloudflared tunnel --url http://localhost:8000
```
Use the generated `https://xxx.trycloudflare.com` URL in the app. Note this URL changes every session.

---

## API Endpoints

All endpoints require Bearer token authentication via `Authorization: Bearer <token>` header, except `/stream` and `/screen` which accept `?token=` query parameter.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/mjpeg?token=` | MJPEG screen stream |
| GET | `/screen?token=` | Screen resolution info |
| GET | `/status` | CPU, RAM, process list |
| POST | `/control/move` | Move mouse to x,y |
| POST | `/control/click` | Click mouse at x,y |
| POST | `/control/key` | Send keyboard input |
| POST | `/control/screenshot` | Save screenshot |
| WS | `/ws/audio?token=` | Real-time audio stream |

---

## Mobile App Controls

### Touch Controls
| Gesture | Action |
|---|---|
| Single tap | Left click |
| Double tap | Double click |
| Drag | Mouse move |
| Pinch | Zoom in/out |
| Two finger drag | Pan view |

### Toolbar Buttons
| Button | Action |
|---|---|
| Win | Windows key |
| Esc | Escape key |
| Enter | Enter key |
| ⌫ | Backspace |
| Win+Tab | Show all open apps |
| Close App | Alt+F4 |
| Scroll Up | Scroll up |
| Scroll Down | Scroll down |
| Keys | Toggle keyboard |
| Off | Disconnect |

---

## Bandwidth Usage

| Connection | Approximate Usage |
|---|---|
| Local WiFi | ~2-4 MB/s |
| Tailscale (same WiFi) | ~2-4 MB/s |
| Tailscale (mobile data) | ~2-4 MB/s (requires good 4G) |
| Cloudflare Tunnel | ~2-4 MB/s |

Minimum recommended: **16 Mbps** download on the receiving device for smooth streaming at 30fps quality 95.

---

## Troubleshooting

**Stream freezes after a while**
The agent automatically restarts the capture thread. Disconnect and reconnect from the app.

**Black screen on connect**
Make sure `android:usesCleartextTraffic="true"` is set in `AndroidManifest.xml`. This is required for HTTP streaming on Android.

**Agent not starting at boot**
Run `schtasks /run /tn "RemoteDesktopAgent"` to test manually. Check that the paths in `start_agent.bat` are correct.

**High latency on mobile data**
This is a bandwidth limitation of mobile networks. Use on WiFi for best experience.

**FFmpeg not found**
Make sure FFmpeg is installed and the `bin` folder is in your system PATH. Run `ffmpeg -version` to verify.

---

## Known Limitations

- Android only (iOS requires HTTPS and additional configuration)
- MJPEG has no audio channel — audio is streamed separately via WebSocket
- High quality streaming requires strong WiFi or fast mobile data
- Remote access requires Tailscale running on both devices

---

## Built With

This project was built entirely from scratch as a personal remote desktop tool. It does not use any third-party remote desktop protocols or SDKs — all screen capture, encoding, streaming, and input control is implemented directly.

---

## License

MIT License — free to use, modify, and distribute.
