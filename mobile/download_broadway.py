import urllib.request
import base64
import os

urls = [
    'https://cdn.jsdelivr.net/npm/broadway-player@0.1.1/Player/Decoder.js',
    'https://cdn.jsdelivr.net/npm/broadway-player@0.1.1/Player/YUVCanvas.js',
    'https://cdn.jsdelivr.net/npm/broadway-player@0.1.1/Player/Player.js'
]

combined = b""
for u in urls:
    print('Downloading', u)
    combined += urllib.request.urlopen(u).read() + b"\n"

b64 = base64.b64encode(combined).decode('utf-8')

out_path = r"c:\Users\kirth\Downloads\Connect\mobile\broadway.ts"
with open(out_path, 'w', encoding='utf-8') as f:
    f.write('export const broadwayBase64 = "')
    f.write(b64)
    f.write('";\n')

print('Done! Saved to', out_path)
