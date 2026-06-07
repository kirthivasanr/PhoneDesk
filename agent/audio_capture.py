import sys
import numpy as np
import soundcard as sc

def main():
    try:
        sp = sc.default_speaker()
        mic = sc.get_microphone(id=sp.id, include_loopback=True)
        with mic.recorder(samplerate=44100, channels=1) as r:
            while True:
                data = r.record(numframes=1024)
                if np.max(np.abs(data)) < 0.0001:
                    continue
                boosted = data * 2.0
                clipped = np.clip(boosted * 32767.0, -32768.0, 32767.0)
                sys.stdout.buffer.write(clipped.astype(np.int16).tobytes())
                sys.stdout.buffer.flush()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)

if __name__ == '__main__':
    main()
