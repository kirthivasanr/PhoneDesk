import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SplashScreen from "expo-splash-screen";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Animated,
  PanResponder,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View
} from "react-native";
import { WebView } from "react-native-webview";

type Connection = {
  url: string;
  token: string;
};

type Point = {
  x: number;
  y: number;
};

type ScreenSize = {
  width: number;
  height: number;
};

const STORAGE_KEY = "connect.remote.settings";
const MIN_ZOOM = 1;
const MAX_ZOOM = 4;

function cleanBaseUrl(value: string) {
  const trimmed = value.trim().replace(/\/+$/, "");
  let url = trimmed;
  if (url.startsWith("//")) url = `http:${url}`;
  else if (!/^https?:\/\//i.test(url)) url = `http://${url}`;
  
  if (url.includes("ngrok") && url.startsWith("http://")) {
    url = url.replace("http://", "https://");
  }
  return url;
}

function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}` };
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function distance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function midpoint(a: Point, b: Point) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

// ---------------------------------------------------------------------------
// Minimal MJPEG viewer HTML — the <img> tag natively renders MJPEG streams
// ---------------------------------------------------------------------------
function getMJPEGHTML(mjpegUrl: string, audioWsUrl: string, token: string): string {
  return `<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
*{margin:0;padding:0}
html,body{width:100%;height:100%;overflow:hidden;background:#000}
img{width:100%;height:100%;object-fit:contain;display:block}
</style>
</head>
<body>
<img id="v" src="${mjpegUrl}?token=${encodeURIComponent(token)}" />
<script>
function msg(t,d){if(window.ReactNativeWebView)window.ReactNativeWebView.postMessage(JSON.stringify(Object.assign({type:t},d||{})));}

var img = document.getElementById('v');
img.onload = function() {
  msg('status', {state: 'Connected'});
};
img.onerror = function() {
  msg('status', {state: 'Connection error'});
  setTimeout(function() {
    img.src = '${mjpegUrl}?token=${encodeURIComponent(token)}&t=' + Date.now();
  }, 3000);
};

window.reconnectStream = function() {
  msg('status', {state: 'Connecting'});
  img.src = '${mjpegUrl}?token=${encodeURIComponent(token)}&t=' + Date.now();
  if (audioWs) {
    audioWs.close();
  }
};

msg('status', {state: 'Connecting'});

// Audio setup
// Unlock Web Audio API automatically without user interaction
var unlockAudio = document.createElement('audio');
unlockAudio.src = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAAAAgA=';
unlockAudio.autoplay = true;
document.body.appendChild(unlockAudio);

var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
var nextPlayTime = 0;
var audioWs = null;

setInterval(function() {
  if (audioCtx.state === 'suspended') {
    audioCtx.resume().catch(function(){});
  }
}, 1000);

function connectAudio() {
  audioWs = new WebSocket('${audioWsUrl}?token=${encodeURIComponent(token)}');
  audioWs.binaryType = 'arraybuffer';
  
  audioWs.onopen = function() {
    msg('status', {state: 'Connected (Audio)'});
  };

  audioWs.onmessage = function(event) {
    if (audioCtx.state === 'suspended') {
        audioCtx.resume().catch(function(){});
    }
    var int16 = new Int16Array(event.data);
    var float32 = new Float32Array(int16.length);
    for (var i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / 32768.0;
    }
    var buffer = audioCtx.createBuffer(1, float32.length, 44100);
    buffer.getChannelData(0).set(float32);
    
    var source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(audioCtx.destination);
    
    var currentTime = audioCtx.currentTime;
    if (nextPlayTime < currentTime) {
        nextPlayTime = currentTime + 0.05; // 50ms buffer
    }
    source.start(nextPlayTime);
    nextPlayTime += buffer.duration;
  };
  
  audioWs.onclose = function(event) {
    msg('status', {state: 'Audio Error ' + event.code});
    setTimeout(connectAudio, 3000);
  };
}

connectAudio();

// Ensure audio context resumes on interaction (fallback)
document.body.addEventListener('touchstart', function() {
  if (audioCtx.state === 'suspended') audioCtx.resume().catch(function(){});
}, {once: true});
</script>
</body>
</html>`;
}

// Keep the splash screen visible while we load stored settings
SplashScreen.preventAutoHideAsync();

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------
export default function App() {
  const [appReady, setAppReady] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [urlInput, setUrlInput] = useState("");
  const [tokenInput, setTokenInput] = useState("");
  const [connectionState, setConnectionState] = useState("Disconnected");
  const [viewSize, setViewSize] = useState({ width: 1, height: 1 });
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState<Point>({ x: 0, y: 0 });
  const [toolbarOpen, setToolbarOpen] = useState(true);
  const [controlMode, setControlMode] = useState(false);
  const [nextClickButton, setNextClickButton] = useState<"left" | "right">("left");
  const [keyboardText, setKeyboardText] = useState("");
  const [screenSize, setScreenSize] = useState<ScreenSize>({ width: 1920, height: 1080 });

  const webViewRef = useRef<WebView | null>(null);
  const keyboardRef = useRef<TextInput | null>(null);
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTap = useRef(0);
  const gestureStart = useRef({
    zoom: 1,
    offset: { x: 0, y: 0 },
    distance: 1,
    midpoint: { x: 0, y: 0 },
    pinching: false
  });
  const moveSentAt = useRef(0);
  const prevTouchCount = useRef(0);
  const zoomRef = useRef(zoom);
  const offsetRef = useRef(offset);
  const viewSizeRef = useRef(viewSize);
  const screenSizeRef = useRef(screenSize);

  // --- Draggable & Fading Toolbar ---
  const toolbarPan = useRef(new Animated.ValueXY({ x: 0, y: 0 })).current;
  const toolbarOpacity = useRef(new Animated.Value(1)).current;
  const fadeTimer = useRef<NodeJS.Timeout | null>(null);

  const wakeToolbar = useCallback(() => {
    if (fadeTimer.current) clearTimeout(fadeTimer.current);
    Animated.timing(toolbarOpacity, {
      toValue: 1,
      duration: 200,
      useNativeDriver: false
    }).start();
    
    fadeTimer.current = setTimeout(() => {
      Animated.timing(toolbarOpacity, {
        toValue: 0.35,
        duration: 500,
        useNativeDriver: false
      }).start();
    }, 3000);
  }, [toolbarOpacity]);

  useEffect(() => {
    wakeToolbar();
    return () => { if (fadeTimer.current) clearTimeout(fadeTimer.current); };
  }, [wakeToolbar]);

  const toolbarPanResponder = useMemo(
    () =>
      PanResponder.create({
        onMoveShouldSetPanResponder: (_, gesture) =>
          Math.abs(gesture.dx) > 5 || Math.abs(gesture.dy) > 5,
        onPanResponderGrant: () => {
          toolbarPan.setOffset({
            x: (toolbarPan.x as any)._value,
            y: (toolbarPan.y as any)._value
          });
          toolbarPan.setValue({ x: 0, y: 0 });
          wakeToolbar();
        },
        onPanResponderMove: Animated.event(
          [null, { dx: toolbarPan.x, dy: toolbarPan.y }],
          { useNativeDriver: false }
        ),
        onPanResponderRelease: () => {
          toolbarPan.flattenOffset();
          wakeToolbar();
        }
      }),
    [toolbarPan, wakeToolbar]
  );

  // ---------------------------------------------------------------------------
  // Sync refs so gesture handlers always see the latest values
  // ---------------------------------------------------------------------------
  useEffect(() => { zoomRef.current = zoom; }, [zoom]);
  useEffect(() => { offsetRef.current = offset; }, [offset]);
  useEffect(() => { viewSizeRef.current = viewSize; }, [viewSize]);
  useEffect(() => { screenSizeRef.current = screenSize; }, [screenSize]);

  // ---------------------------------------------------------------------------
  // Restore saved connection on mount, then hide splash screen
  // ---------------------------------------------------------------------------
  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY).then((saved) => {
      if (saved) {
        const parsed = JSON.parse(saved) as Connection;
        setConnection(parsed);
        setUrlInput(parsed.url);
        setTokenInput(parsed.token);
      }
    }).finally(() => {
      setAppReady(true);
      SplashScreen.hideAsync();
    });
  }, []);

  // ---------------------------------------------------------------------------
  // Fetch screen dimensions from the agent when the connection changes
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (!connection) return;
    setConnectionState("Connecting");

    fetch(`${connection.url}/screen?token=${encodeURIComponent(connection.token)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return res.json();
      })
      .then((data: ScreenSize) => {
        setScreenSize(data);
      })
      .catch(() => {});
  }, [connection]);

  // ---------------------------------------------------------------------------
  // Handle messages from the WebView (connection state updates)
  // ---------------------------------------------------------------------------
  const handleWebViewMessage = useCallback((event: any) => {
    try {
      const data = JSON.parse(event.nativeEvent.data);
      if (data.type === "status") {
        setConnectionState(data.state);
      }
    } catch {}
  }, []);

  // ---------------------------------------------------------------------------
  // Stream URLs
  // ---------------------------------------------------------------------------
  const mjpegUrl = connection
    ? `${connection.url}/mjpeg`
    : null;

  const audioWsUrl = connection
    ? `${connection.url.replace(/^http/, "ws")}/ws/audio`
    : null;

  // ---------------------------------------------------------------------------
  // Geometry: maps the remote screen onto the local view
  // ---------------------------------------------------------------------------
  const imageRect = useMemo(() => {
    const remoteWidth  = screenSize.width  || 1;
    const remoteHeight = screenSize.height || 1;
    const scale  = Math.min(viewSize.width / remoteWidth, viewSize.height / remoteHeight);
    const width  = remoteWidth  * scale;
    const height = remoteHeight * scale;

    return {
      x: (viewSize.width  - width)  / 2,
      y: (viewSize.height - height) / 2,
      width,
      height
    };
  }, [screenSize.height, screenSize.width, viewSize.height, viewSize.width]);

  const clampOffset = useCallback(
    (candidate: Point, nextZoom = zoom) => {
      const maxX = Math.max(0, (imageRect.width * nextZoom - viewSize.width) / 2);
      const maxY = Math.max(0, (imageRect.height * nextZoom - viewSize.height) / 2);
      return {
        x: clamp(candidate.x, -maxX, maxX),
        y: clamp(candidate.y, -maxY, maxY)
      };
    },
    [imageRect.height, imageRect.width, viewSize.height, viewSize.width, zoom]
  );

  // ---------------------------------------------------------------------------
  // Network helpers
  // ---------------------------------------------------------------------------
  const sendPost = useCallback(
    async (path: string, body: unknown = {}) => {
      if (!connection) return null;
      const response = await fetch(`${connection.url}${path}`, {
        method: "POST",
        headers: {
          ...authHeaders(connection.token),
          "Content-Type": "application/json"
        },
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      return response.json().catch(() => null);
    },
    [connection]
  );

  // Maps a touch point in the viewer to remote screen coordinates.
  const mapPoint = useCallback(
    (x: number, y: number) => {
      const z   = zoomRef.current;
      const off = offsetRef.current;
      const vs  = viewSizeRef.current;
      const ss  = screenSizeRef.current;

      const remoteWidth  = ss.width;
      const remoteHeight = ss.height;
      const scale     = Math.min(vs.width / remoteWidth, vs.height / remoteHeight);
      const imgWidth  = remoteWidth  * scale;
      const imgHeight = remoteHeight * scale;
      const imgX = (vs.width  - imgWidth)  / 2;
      const imgY = (vs.height - imgHeight) / 2;

      const cx = vs.width  / 2;
      const cy = vs.height / 2;
      const contentX = (x - cx - off.x) / z + cx;
      const contentY = (y - cy - off.y) / z + cy;

      return {
        x: clamp(Math.round(((contentX - imgX) / imgWidth)  * remoteWidth),  0, remoteWidth  - 1),
        y: clamp(Math.round(((contentY - imgY) / imgHeight) * remoteHeight), 0, remoteHeight - 1)
      };
    },
    []
  );

  const sendClick = useCallback(
    (x: number, y: number, clicks: 1 | 2) => {
      const point = mapPoint(x, y);
      const button = nextClickButton;
      setNextClickButton("left");
      sendPost("/control/click", { ...point, button, clicks }).catch(() => undefined);
    },
    [mapPoint, nextClickButton, sendPost]
  );

  // ---------------------------------------------------------------------------
  // PanResponder
  // ---------------------------------------------------------------------------
  const panResponder = useMemo(
    () =>
      PanResponder.create({
        onStartShouldSetPanResponder: () => true,
        onMoveShouldSetPanResponder: () => true,

        onPanResponderGrant: (event) => {
          const touches = event.nativeEvent.touches;
          const first = { x: touches[0]?.locationX || 0, y: touches[0]?.locationY || 0 };
          gestureStart.current = {
            zoom: zoomRef.current,
            offset: { ...offsetRef.current },
            distance: 1,
            midpoint: { ...first },
            pinching: false
          };
          prevTouchCount.current = touches.length;
        },

        onPanResponderMove: (event, gesture) => {
          const touches = event.nativeEvent.touches;
          const currentCount = touches.length;
          const vs = viewSizeRef.current;

          // --- Pinch-to-zoom ---
          if (currentCount >= 2) {
            const f1 = { x: touches[0].locationX, y: touches[0].locationY };
            const f2 = { x: touches[1].locationX, y: touches[1].locationY };
            const currentDist = Math.max(1, distance(f1, f2));
            const currentMid  = midpoint(f1, f2);

            if (prevTouchCount.current < 2) {
              gestureStart.current = {
                zoom: zoomRef.current,
                offset: { ...offsetRef.current },
                distance: currentDist,
                midpoint: { ...currentMid },
                pinching: true
              };
              prevTouchCount.current = currentCount;
              return;
            }

            prevTouchCount.current = currentCount;
            const start = gestureStart.current;
            start.pinching = true;

            const nextZoom = clamp((currentDist / start.distance) * start.zoom, MIN_ZOOM, MAX_ZOOM);
            const center = { x: vs.width / 2, y: vs.height / 2 };
            const focusContent = {
              x: (start.midpoint.x - center.x - start.offset.x) / start.zoom,
              y: (start.midpoint.y - center.y - start.offset.y) / start.zoom
            };
            const rawOffset = {
              x: currentMid.x - center.x - focusContent.x * nextZoom,
              y: currentMid.y - center.y - focusContent.y * nextZoom
            };

            const ss = screenSizeRef.current;
            const sc = Math.min(vs.width / ss.width, vs.height / ss.height);
            const maxX = Math.max(0, (ss.width  * sc * nextZoom - vs.width)  / 2);
            const maxY = Math.max(0, (ss.height * sc * nextZoom - vs.height) / 2);

            setZoom(nextZoom);
            setOffset({ x: clamp(rawOffset.x, -maxX, maxX), y: clamp(rawOffset.y, -maxY, maxY) });
            return;
          }

          prevTouchCount.current = currentCount;

          // --- Mouse-move mode ---
          if (controlMode) {
            const now = Date.now();
            if (now - moveSentAt.current < 45) return;
            moveSentAt.current = now;
            const point = mapPoint(event.nativeEvent.locationX, event.nativeEvent.locationY);
            sendPost("/control/move", point).catch(() => undefined);
            return;
          }

          // --- Pan when zoomed in ---
          const z = zoomRef.current;
          if (z > 1) {
            const start = gestureStart.current;
            const ss = screenSizeRef.current;
            const sc = Math.min(vs.width / ss.width, vs.height / ss.height);
            const maxX = Math.max(0, (ss.width  * sc * z - vs.width)  / 2);
            const maxY = Math.max(0, (ss.height * sc * z - vs.height) / 2);
            setOffset({
              x: clamp(start.offset.x + gesture.dx, -maxX, maxX),
              y: clamp(start.offset.y + gesture.dy, -maxY, maxY)
            });
          }
        },

        onPanResponderRelease: (event, gesture) => {
          const wasPinching = gestureStart.current.pinching;
          prevTouchCount.current = 0;
          gestureStart.current.pinching = false;

          if (wasPinching || Math.abs(gesture.dx) > 8 || Math.abs(gesture.dy) > 8) return;

          const now = Date.now();
          const isDoubleTap = now - lastTap.current < 300;
          lastTap.current = now;
          const { locationX, locationY } = event.nativeEvent;

          if (isDoubleTap) {
            if (clickTimer.current) clearTimeout(clickTimer.current);
            sendClick(locationX, locationY, 2);
            return;
          }

          clickTimer.current = setTimeout(() => {
            sendClick(locationX, locationY, 1);
          }, 300);
        },

        onPanResponderTerminate: () => {
          prevTouchCount.current = 0;
          gestureStart.current.pinching = false;
        }
      }),
    [controlMode, mapPoint, sendClick, sendPost]
  );

  // Keep offset clamped when zoom changes
  useEffect(() => {
    setOffset((current) => clampOffset(current, zoom));
  }, [clampOffset, zoom]);

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------
  const saveLogin = async () => {
    const next = { url: cleanBaseUrl(urlInput), token: tokenInput.trim() };
    await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setConnection(next);
  };

  const disconnect = async () => {
    if (clickTimer.current) clearTimeout(clickTimer.current);
    setConnection(null);
    setScreenSize({ width: 1920, height: 1080 });
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    setConnectionState("Disconnected");
  };

  const reconnect = () => {
    if (!connection) return;
    webViewRef.current?.injectJavaScript("window.reconnectStream(); true;");
    setConnectionState("Connecting");
  };

  const sendKeyboardText = (nextText: string) => {
    const previous = keyboardText;
    setKeyboardText(nextText);
    if (nextText.length > previous.length) {
      const typed = nextText.slice(previous.length);
      sendPost("/control/key", { text: typed }).catch(() => undefined);
    }
  };

  const sendSpecialKey = (key: string) => {
    sendPost("/control/key", { key }).catch(() => undefined);
  };

  const sendHotkey = (keys: string[]) => {
    sendPost("/control/hotkey", { keys }).catch(() => undefined);
  };

  const sendScroll = (clicks: number) => {
    sendPost("/control/scroll", { clicks }).catch(() => undefined);
  };

  const toggleKeyboard = () => {
    if (keyboardRef.current?.isFocused()) {
      keyboardRef.current.blur();
      return;
    }
    keyboardRef.current?.focus();
  };

  const takeScreenshot = async () => {
    await sendPost("/control/screenshot").catch(() => undefined);
  };

  // ---------------------------------------------------------------------------
  // Login screen
  // ---------------------------------------------------------------------------
  if (!connection) {
    return (
      <SafeAreaView style={styles.login}>
        <Text style={styles.title}>Connect Remote</Text>
        <TextInput
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          onChangeText={setUrlInput}
          placeholder="https://your-ngrok-url"
          placeholderTextColor="#7a8597"
          style={styles.input}
          value={urlInput}
        />
        <TextInput
          autoCapitalize="none"
          autoCorrect={false}
          onChangeText={setTokenInput}
          placeholder="Bearer token"
          placeholderTextColor="#7a8597"
          secureTextEntry
          style={styles.input}
          value={tokenInput}
        />
        <Pressable style={styles.primaryButton} onPress={saveLogin}>
          <Text style={styles.primaryText}>Connect</Text>
        </Pressable>
      </SafeAreaView>
    );
  }

  // ---------------------------------------------------------------------------
  // Viewer screen
  // ---------------------------------------------------------------------------
  return (
    <View style={styles.screen}>
      <StatusBar hidden />
      <View
        style={styles.viewer}
        {...panResponder.panHandlers}
        onLayout={(event) => setViewSize(event.nativeEvent.layout)}
      >
        {/* WebView renders an <img> pointing at the MJPEG stream.
            Android WebView (Chrome) natively supports multipart/x-mixed-replace.
            pointerEvents="none" ensures all touches go to the PanResponder. */}
        <View
          style={[
            styles.videoFrame,
            { transform: [{ translateX: offset.x }, { translateY: offset.y }, { scale: zoom }] }
          ]}
          pointerEvents="none"
        >
          {mjpegUrl && audioWsUrl && connection && (
            <WebView
              ref={webViewRef}
              source={{ html: getMJPEGHTML(mjpegUrl, audioWsUrl, connection.token) }}
              style={styles.webview}
              originWhitelist={["*"]}
              scrollEnabled={false}
              bounces={false}
              overScrollMode="never"
              javaScriptEnabled={true}
              mediaPlaybackRequiresUserAction={false}
              allowsInlineMediaPlayback={true}
              mixedContentMode="always"
              allowUniversalAccessFromFileURLs={true}
              androidLayerType="hardware"
              onMessage={handleWebViewMessage}
              onLoad={() => {
                // Force the img tag to repaint immediately on Android WebView
                // Without this, the first frame stays invisible until a touch event
                webViewRef.current?.injectJavaScript(`
                  (function() {
                    var img = document.getElementById('v');
                    if (img) {
                      var src = img.src;
                      img.src = '';
                      setTimeout(function() { img.src = src; }, 50);
                    }
                  })();
                  true;
                `);
              }}
            />
          )}
        </View>
        {connectionState === "Connecting" && <ActivityIndicator color="#dbe7ff" />}
      </View>

      <Animated.View
        {...toolbarPanResponder.panHandlers}
        onTouchStart={wakeToolbar}
        style={[
          styles.toolbarContainer,
          {
            transform: toolbarPan.getTranslateTransform(),
            opacity: toolbarOpacity
          }
        ]}
      >
        <Pressable onPress={() => setToolbarOpen((open) => !open)} style={styles.toolbarToggle}>
          <Text style={styles.toolText}>{toolbarOpen ? "›" : "‹"}</Text>
        </Pressable>
        {toolbarOpen && (
          <ScrollView style={styles.toolbarScroll} contentContainerStyle={styles.toolbarGrid} showsVerticalScrollIndicator={false}>
            <Pressable onPress={() => sendSpecialKey("win")} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>⊞ Win</Text>
            </Pressable>
            <Pressable onPress={() => sendSpecialKey("esc")} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>Esc</Text>
            </Pressable>
            <Pressable onPress={() => sendSpecialKey("enter")} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>↵ Enter</Text>
            </Pressable>
            <Pressable onPress={() => sendSpecialKey("backspace")} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>⌫ Backspace</Text>
            </Pressable>
            <Pressable onPress={() => sendHotkey(["win", "tab"])} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>⊞+⇥ Win+Tab</Text>
            </Pressable>
            <Pressable onPress={() => sendHotkey(["win", "d"])} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>− Minimize</Text>
            </Pressable>
            <Pressable onPress={() => sendHotkey(["alt", "f4"])} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>✕ Close App</Text>
            </Pressable>
            <Pressable onPress={() => sendScroll(3)} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>↑ Scroll Up</Text>
            </Pressable>
            <Pressable onPress={() => sendScroll(-3)} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>↓ Scroll Down</Text>
            </Pressable>
            <Pressable onPress={toggleKeyboard} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>⌨ Keys</Text>
            </Pressable>
            <Pressable onPress={disconnect} style={({ pressed }) => [styles.gridButton, pressed && styles.gridButtonPressed]}>
              <Text style={styles.gridButtonText}>⏻ Off</Text>
            </Pressable>
          </ScrollView>
        )}
      </Animated.View>

      <TextInput
        ref={keyboardRef}
        autoCapitalize="none"
        autoCorrect={false}
        blurOnSubmit={false}
        onChangeText={sendKeyboardText}
        onKeyPress={(event) => {
          if (event.nativeEvent.key === "Backspace") sendSpecialKey("backspace");
          if (event.nativeEvent.key === "Enter") sendSpecialKey("enter");
        }}
        style={styles.hiddenKeyboard}
        value={keyboardText}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  login: {
    flex: 1,
    justifyContent: "center",
    padding: 24,
    backgroundColor: "#f5f7fb"
  },
  title: {
    fontSize: 28,
    fontWeight: "700",
    marginBottom: 24,
    color: "#172033"
  },
  input: {
    minHeight: 48,
    borderWidth: 1,
    borderColor: "#c9d1df",
    borderRadius: 8,
    paddingHorizontal: 14,
    marginBottom: 12,
    backgroundColor: "#fff",
    color: "#172033"
  },
  primaryButton: {
    minHeight: 48,
    borderRadius: 8,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#176b87"
  },
  primaryText: {
    color: "#fff",
    fontWeight: "700"
  },
  screen: {
    flex: 1,
    backgroundColor: "#05070d"
  },
  viewer: {
    flex: 1,
    backgroundColor: "#05070d",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden"
  },
  videoFrame: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0
  },
  webview: {
    flex: 1,
    backgroundColor: "#000"
  },
  statusBadge: {
    position: "absolute",
    top: 14,
    left: 14,
    maxWidth: 260,
    minHeight: 34,
    borderRadius: 8,
    paddingHorizontal: 12,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(10, 16, 28, 0.72)"
  },
  statusBadgeText: {
    color: "#eff6ff",
    fontSize: 12,
    fontWeight: "700"
  },
  toolbarContainer: {
    position: "absolute",
    right: 12,
    top: 76,
    maxHeight: "80%",
    backgroundColor: "rgba(30, 30, 46, 0.85)", // dark blurred appearance
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#313244",
    padding: 4,
    shadowColor: "#000",
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.4,
    shadowRadius: 12,
    elevation: 8,
  },
  toolbarToggle: {
    alignSelf: "flex-end", // Align to right when expanded
    width: 32,
    height: 32,
    borderRadius: 6,
    backgroundColor: "rgba(69, 71, 90, 0.5)",
    alignItems: "center",
    justifyContent: "center",
  },
  toolbarScroll: {
    marginTop: 4,
    width: 160, // Slim 2-column width
  },
  toolbarGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "space-between",
  },
  gridButton: {
    width: "48%", // 2 columns with tiny gap
    height: 34,
    borderRadius: 6,
    backgroundColor: "#1E1E2E",
    borderWidth: 1,
    borderColor: "#313244",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 4,
    shadowColor: "#000",
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 4,
    elevation: 3,
  },
  gridButtonPressed: {
    backgroundColor: "#45475A",
  },
  gridButtonText: {
    color: "#FFFFFF",
    fontSize: 9,
    fontWeight: "600",
    textAlign: "center",
  },
  toolText: {
    color: "#f8fafc",
    fontSize: 12,
    fontWeight: "800"
  },
  hiddenKeyboard: {
    position: "absolute",
    left: -20,
    bottom: -20,
    width: 1,
    height: 1,
    opacity: 0
  }
});
