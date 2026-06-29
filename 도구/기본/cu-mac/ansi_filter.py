#!/opt/homebrew/bin/python3.13
"""
ANSI/VT100 이스케이프 코드 제거 필터
stdin → stdout (라인 버퍼, 한국어 포함 UTF-8 지원)
"""
import sys
import re

# CSI, OSC, DCS, 단일 문자 ESC 시퀀스 포괄
_ANSI = re.compile(
    r'\x1b(?:'
    r'\[[0-?]*[ -/]*[@-~]'    # CSI: ESC [ ... <final>
    r'|\][^\x07\x1b]*[\x07]'  # OSC: ESC ] ... BEL
    r'|[@-Z\\-_]'             # Fe: ESC @~_ (2-char)
    r'|[PX^_][^\x9c]*\x9c'   # DCS/SOS/PM/APC
    r')'
)

def strip(text: str) -> str:
    s = _ANSI.sub('', text)
    s = s.replace('\r\n', '\n').replace('\r', '\n')
    return s

stdin  = open(sys.stdin.fileno(),  'rb', buffering=0)
stdout = open(sys.stdout.fileno(), 'wb', buffering=0)

buf = b''
while True:
    chunk = stdin.read(256)
    if not chunk:
        break
    buf += chunk
    text = buf.decode('utf-8', errors='replace')
    buf  = b''
    cleaned = strip(text)
    if cleaned:
        stdout.write(cleaned.encode('utf-8'))
