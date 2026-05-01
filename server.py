#!/usr/bin/env python3
"""
Wi-SUN Bルート 電力モニター - バックエンドサーバー
FastAPI + Wi-SUN読み取りを統合

依存: pip install fastapi uvicorn pyserial
起動: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import serial
import time
import threading
import sys
from collections import deque
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

# ============================================================
# ★ 設定
# ============================================================
BROUTE_ID = "00000099021A00000000000000D6360D"  # BルートID (32文字)
BROUTE_PWD = "LR2UJIKLM6HQ" # Bルートパスワード (12文字)
SERIAL_PORT = "/dev/tty.usbserial-DJ007AIG"   # ラズパイなら /dev/ttyUSB0
BAUD_RATE   = 115200

CONTRACT_A  = 50          # 契約アンペア
VOLTAGE     = 220         # 電圧 (V) — 単相3線 100V/200V 混在だが概算で220V換算
MAX_WATT    = CONTRACT_A * VOLTAGE  # 50A × 220V = 11000W

POLL_INTERVAL = 10        # 取得間隔（秒）
HISTORY_SIZE  = 360       # 保持するデータ数（360 × 10秒 = 1時間分）
# ============================================================

# ECHONET Lite フレーム（瞬時電力 0xE7 + 瞬時電流 0xE8）
EL_FRAME = bytes([
    0x10, 0x81,
    0x00, 0x01,
    0x05, 0xFF, 0x01,
    0x02, 0x88, 0x01,
    0x62,
    0x02,        # OPC: 2プロパティ
    0xE7, 0x00,  # 瞬時電力
    0xE8, 0x00,  # 瞬時電流
])

# グローバル状態
state = {
    "watt": None,
    "ampere_r": None,   # R相電流 (0.1A単位)
    "ampere_t": None,   # T相電流 (0.1A単位)
    "updated_at": None,
    "status": "connecting",   # connecting / ok / error
    "history": deque(maxlen=HISTORY_SIZE),
}
state_lock = threading.Lock()

@asynccontextmanager
async def lifespan(application):
    t = threading.Thread(target=wisun_worker, daemon=True)
    t.start()
    print("[server] Wi-SUNワーカースレッド起動", file=sys.stderr)
    yield

app = FastAPI(lifespan=lifespan)


# ─── Wi-SUN 通信 ──────────────────────────────────────────

def open_serial():
    return serial.Serial(
        SERIAL_PORT, baudrate=BAUD_RATE,
        bytesize=8, parity=serial.PARITY_NONE,
        stopbits=1, timeout=10,
    )


def send_cmd(ser, cmd: str):
    ser.write((cmd + "\r\n").encode())


def readline(ser) -> str:
    raw = ser.readline()
    return raw.decode(errors="replace").strip() if raw else ""


def wait_for(ser, keyword: str, timeout: float = 15.0) -> list[str]:
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = readline(ser)
        if line:
            lines.append(line)
            if keyword in line:
                return lines
    raise TimeoutError(f"'{keyword}' が受信できませんでした")


def scan_and_join(ser) -> str:
    send_cmd(ser, f"SKSETPWD C {BROUTE_PWD}")
    wait_for(ser, "OK")
    send_cmd(ser, f"SKSETRBID {BROUTE_ID}")
    wait_for(ser, "OK")

    send_cmd(ser, "SKSCAN 2 FFFFFFFF 6")
    lines = wait_for(ser, "EVENT 22", timeout=60.0)

    channel = pan_id = mac_addr = None
    for line in lines:
        if "Channel:" in line:
            channel = line.split(":")[1].strip()
        elif "Pan ID:" in line:
            pan_id = line.split(":")[1].strip()
        elif "Addr:" in line:
            mac_addr = line.split(":")[1].strip()

    if not all([channel, pan_id, mac_addr]):
        raise RuntimeError("スマートメータが見つかりませんでした")

    send_cmd(ser, f"SKSREG S2 {channel}")
    wait_for(ser, "OK")
    send_cmd(ser, f"SKSREG S3 {pan_id}")
    wait_for(ser, "OK")

    send_cmd(ser, f"SKLL64 {mac_addr}")
    lines = wait_for(ser, "FE80", timeout=5.0)
    ipv6 = next((l for l in lines if l.startswith("FE80")), None)
    if not ipv6:
        raise RuntimeError("IPv6アドレスの取得に失敗しました")

    send_cmd(ser, f"SKJOIN {ipv6}")
    wait_for(ser, "OK")

    deadline = time.time() + 90.0
    while time.time() < deadline:
        line = readline(ser)
        if "EVENT 25" in line:
            return ipv6
        if "EVENT 24" in line:
            raise RuntimeError("PANA認証失敗")

    raise TimeoutError("PANA認証がタイムアウトしました")


def request_power(ser, ipv6: str):
    datalen = f"{len(EL_FRAME):04X}"
    header = f"SKSENDTO 1 {ipv6} 0E1A 1 {datalen} "
    ser.write(header.encode() + EL_FRAME + b"\r\n")

    deadline = time.time() + 10.0
    while time.time() < deadline:
        line = readline(ser)
        if line.startswith("ERXUDP"):
            parts = line.split(" ")
            if len(parts) >= 9:
                return parse_el(parts[-1])
    return None, None, None


def parse_el(hex_str: str):
    """ELレスポンスから (watt, ampere_r, ampere_t) を返す"""
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        return None, None, None

    if len(data) < 12 or data[10] != 0x72:
        return None, None, None

    watt = ampere_r = ampere_t = None
    idx = 12
    opc = data[11]
    for _ in range(opc):
        if idx + 2 > len(data):
            break
        epc, pdc = data[idx], data[idx + 1]
        idx += 2
        if epc == 0xE7 and pdc == 4:
            watt = int.from_bytes(data[idx:idx+4], "big", signed=True)
        elif epc == 0xE8 and pdc == 4:
            ampere_r = int.from_bytes(data[idx:idx+2], "big", signed=True)
            ampere_t = int.from_bytes(data[idx+2:idx+4], "big", signed=True)
        idx += pdc

    return watt, ampere_r, ampere_t


# ─── バックグラウンドスレッド ────────────────────────────

def wisun_worker():
    """Wi-SUNの接続と定期取得をバックグラウンドで行う"""
    while True:
        try:
            with state_lock:
                state["status"] = "connecting"

            ser = open_serial()
            send_cmd(ser, "SKRESET")
            wait_for(ser, "OK")

            ipv6 = scan_and_join(ser)

            with state_lock:
                state["status"] = "ok"

            while True:
                watt, ar, at = request_power(ser, ipv6)
                now = datetime.now()
                with state_lock:
                    if watt is not None:
                        state["watt"] = watt
                        state["ampere_r"] = ar
                        state["ampere_t"] = at
                        state["updated_at"] = now.isoformat()
                        state["status"] = "ok"
                        state["history"].append({
                            "t": now.strftime("%H:%M:%S"),
                            "w": watt,
                        })
                time.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"[wisun_worker エラー] {e}", file=sys.stderr)
            with state_lock:
                state["status"] = "error"
            time.sleep(15)


# ─── API ──────────────────────────────────────────────────

@app.get("/api/power")
def api_power():
    with state_lock:
        watt = state["watt"]
        ar   = state["ampere_r"]
        at_  = state["ampere_t"]
        pct  = round(watt / MAX_WATT * 100, 1) if watt is not None else None
        remaining_w = (MAX_WATT - watt) if watt is not None else None

        # 家電の目安 (W)
        APPLIANCES = [
            {"name": "ドライヤー（強）", "watt": 1200},
            {"name": "電子レンジ",       "watt": 1400},
            {"name": "エアコン（暖房）", "watt": 1500},
            {"name": "IHクッキングヒーター", "watt": 3000},
            {"name": "電気ケトル",       "watt": 1300},
            {"name": "掃除機",           "watt": 600},
            {"name": "炊飯器",           "watt": 1450},
            {"name": "洗濯乾燥機",       "watt": 1400},
        ]

        warnings = []
        suggestions = []
        if remaining_w is not None:
            for a in APPLIANCES:
                if a["watt"] > remaining_w:
                    warnings.append(a["name"])
                else:
                    suggestions.append(a["name"])

        history = list(state["history"])

    return {
        "watt":        watt,
        "ampere_r":    round(ar / 10, 1) if ar is not None else None,
        "ampere_t":    round(at_ / 10, 1) if at_ is not None else None,
        "pct":         pct,
        "max_watt":    MAX_WATT,
        "contract_a":  CONTRACT_A,
        "remaining_w": remaining_w,
        "updated_at":  state["updated_at"],
        "status":      state["status"],
        "warnings":    warnings,
        "suggestions": suggestions,
        "history":     history[-60:],   # 直近60件（10分）
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html", encoding="utf-8") as f:
        return f.read()


# ─── 起動 ─────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)