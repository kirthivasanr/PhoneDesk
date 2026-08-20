# PhoneDesk — Remote Desktop from Your Phone

Control and monitor a Windows PC remotely from an Android phone.

PhoneDesk
consists of a **Python/FastAPI agent** running on the PC and a **React Native/Expo Android app**. The system provides live screen streaming, remote input, system monitoring, and remote connectivity.

## Features

* 📺 Live PC screen streaming
* 🖱️ Touch-based mouse control
* ⌨️ Remote keyboard input
* 📊 CPU, RAM, and process monitoring
* 📸 Remote screenshots
* 🔊 Real-time audio streaming
* 🖥️ Windows system-tray agent
* 🔄 Automatic startup support
* 🌐 Local-network and remote connectivity
* 🚀 Smooth screen streaming up to 60 FPS

## Architecture

```text
Android App
     │
     │ HTTP / WebSocket
     ▼
Tailscale / Local Network
     │
     ▼
Python FastAPI Agent
     │
     ├── Screen Capture
     ├── JPEG / MJPEG Streaming
     ├── Mouse & Keyboard Control
     ├── System Monitoring
     └── Audio Streaming
```

### Video Streaming

The PC captures the screen and encodes frames as JPEG images. Frames are delivered to the Android app as an **MJPEG HTTP stream**.

The current implementation focuses on keeping the stream smooth and responsive while avoiding unnecessary frame transmission.

### Remote Connectivity

For remote access, **Tailscale** can provide connectivity between the Android phone and PC without requiring a public IP or manual port forwarding.

Local-network connections can also be used when the phone and PC are on the same network.

## Tech Stack

### PC Agent

| Component         | Technology           |
| ----------------- | -------------------- |
| API               | FastAPI + Uvicorn    |
| Screen Capture    | DXCam / MSS fallback |
| Image Encoding    | OpenCV / JPEG        |
| Input Control     | PyAutoGUI            |
| System Monitoring | psutil               |
| System Tray       | pystray              |
| Audio             | sounddevice          |
| Language          | Python 3.13          |

### Android App

| Component       | Technology                    |
| --------------- | ----------------------------- |
| Framework       | React Native                  |
| Platform        | Expo                          |
| Language        | TypeScript                    |
| Video Rendering | WebView + MJPEG               |
| Touch Input     | React Native gesture handling |
| Android Build   | EAS Build                     |

### Connectivity

* Local network
* Tailscale
* Cloudflare Tunnel for testing/backup connectivity

## Project Structure

```text
Connect/
├── agent/
│   ├── agent.py
│   ├── requirements.txt
│   └── ...
│
└── mobile/
    ├── App.tsx
    ├── app.json
    ├── eas.json
    ├── package.json
    ├── package-lock.json
    ├── assets/
    └── android/
```

## Requirements

### PC

* Windows 10/11
* Python 3.13
* FFmpeg
* Tailscale
* Required Python packages from `agent/requirements.txt`

### Android

* Android 8.0+ recommended
* Tailscale when using remote access
* Connect APK

### Development

* Node.js
* npm
* Expo / EAS CLI

**Android Studio is not required for EAS cloud builds.**

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/kirthivasanr/PhoneDesk.git
cd Connect
```

### 2. Set Up the PC Agent

```powershell
cd agent

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Install FFmpeg

Install FFmpeg and make sure it is available through the system PATH.

Verify:

```powershell
ffmpeg -version
```

### 4. Configure Authentication

Do not commit real authentication tokens or secrets to Git.

Set the required authentication token using the project's supported environment/configuration mechanism.

### 5. Install the Mobile Dependencies

```powershell
cd ..\mobile
npm install
```

## Running the Agent

From the `agent` directory:

```powershell
.venv\Scripts\python.exe agent.py
```

The agent starts the FastAPI server and system-tray interface.

## Building the Android APK

PhoneDesk uses **EAS Build** for cloud-based Android builds.

Install EAS CLI:

```powershell
npm install -g eas-cli
```

Log in:

```powershell
eas login
```

Configure EAS if required:

```powershell
eas build:configure
```

Build an installable APK:

```powershell
eas build --platform android --profile preview
```

The completed build provides an APK that can be downloaded and installed directly on an Android device.

## Connecting the Android App

### Local Network

When the phone and PC are on the same network, use the PC's local IP address:

```text
http://<PC-IP>:8000
```

### Tailscale

For remote access:

1. Install Tailscale on both devices.
2. Sign in to the same Tailscale network.
3. Start the PC agent.
4. Enter the PC's Tailscale IP in PhoneDesk.

Example:

```text
http://100.x.x.x:8000
```

Enter the configured authentication token and connect.

## API

The agent exposes HTTP and WebSocket endpoints for communication between the Android app and PC.

| Method | Endpoint              | Purpose            |
| ------ | --------------------- | ------------------ |
| GET    | `/mjpeg`              | Live screen stream |
| GET    | `/screen`             | Screen information |
| GET    | `/status`             | System status      |
| POST   | `/control/move`       | Mouse movement     |
| POST   | `/control/click`      | Mouse click        |
| POST   | `/control/key`        | Keyboard input     |
| POST   | `/control/screenshot` | Screenshot         |
| WS     | `/ws/audio`           | Audio stream       |

Authentication is required for protected endpoints.

## Controls

| Gesture / Control | Action                   |
| ----------------- | ------------------------ |
| Single tap        | Left click               |
| Double tap        | Double click             |
| Drag              | Mouse movement           |
| Pinch             | Zoom                     |
| Two-finger drag   | Pan                      |
| Win               | Windows key              |
| Esc               | Escape                   |
| Enter             | Enter                    |
| Backspace         | Backspace                |
| Win+Tab           | Switch/open Windows view |
| Alt+F4            | Close active application |
| Scroll Up         | Scroll up                |
| Scroll Down       | Scroll down              |
| Keys              | Toggle keyboard          |
| Off               | Disconnect               |

## Performance

The project is designed for responsive remote desktop viewing.

Actual performance depends on:

* Screen resolution
* JPEG quality
* Frame rate
* PC performance
* Network bandwidth
* Network latency
* Tailscale connectivity path
* Mobile network conditions

Local Wi-Fi generally provides the best experience. Remote mobile connections may require lower streaming quality or frame rate depending on network conditions.

## Troubleshooting

### Stream is frozen

Restart the connection from the Android app and verify that the PC agent is running.

### Cannot connect remotely

Check that:

* Tailscale is connected on both devices.
* The PC agent is running.
* The correct Tailscale IP is being used.
* The configured port is reachable.

### FFmpeg is not detected

Run:

```powershell
ffmpeg -version
```

If the command is not recognized, add FFmpeg's `bin` directory to the system PATH.

### Android build fails

Check the EAS build logs:

```powershell
eas build:list
```

Then inspect the relevant build with:

```powershell
eas build:view <BUILD_ID>
```

## Current Limitations

* Android-focused implementation
* MJPEG uses more bandwidth than modern inter-frame video codecs
* Remote streaming performance depends heavily on network quality
* Tailscale connectivity may vary depending on whether a direct or relayed connection is established
* iOS support is not currently provided

## Development Status

PhoneDesk is an actively developed personal remote-desktop project.

Current development priorities include:

* Improving remote-network performance
* Improving streaming stability
* Optimizing bandwidth usage
* Improving Android experience
* Further refining the PC agent

## License

MIT License.
