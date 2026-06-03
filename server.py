#!/usr/bin/env python3
"""
Wi-SUN Bルート 電力モニター - バックエンドサーバー
- 30秒ポーリング
- RAM (deque) + SQLite 二重保存
- 積算電力量（当月分）対応
- /api/power     : 最新値
- /api/history   : チャート用履歴 (?range=1h or 24h)

依存: pip install fastapi uvicorn pyserial
起動: python server.py
"""

import serial
import time
import threading
import sys
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import uvicorn

# ============================================================
# ★ 設定
# ============================================================
BROUTE_ID  = "00000099021A00000000000000D6360D"
BROUTE_PWD = "LR2UJIKLM6HQ"
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE   = 115200

CONTRACT_A = 50
VOLTAGE    = 220
MAX_WATT   = 5000

POLL_INTERVAL = 30
RAM_SIZE      = 2880  # 2880 × 30秒 = 1日分
DB_PATH       = "power.db"
DB_KEEP_DAYS  = 2
# ============================================================

# ECHONET Lite フレーム（瞬時電力 0xE7 + 瞬時電流 0xE8 + 積算電力量 0xE0）
EL_FRAME = bytes([
    0x10, 0x81, 0x00, 0x01,
    0x05, 0xFF, 0x01,
    0x02, 0x88, 0x01,
    0x62,        # GET
    0x03,        # OPC: 3プロパティ
    0xE7, 0x00,  # 瞬時電力
    0xE8, 0x00,  # 瞬時電流
    0xE0, 0x00,  # 積算電力量（正方向）
])

# ─── グローバル状態
state = {
    "watt":       None,
    "ampere_r":   None,
    "ampere_t":   None,
    "kwh_total":  None,   # スマートメータ生の積算値 (kWh)
    "kwh_month":  None,   # 当月消費 (kWh)
    "updated_at": None,
    "status":     "connecting",
    "ram":        deque(maxlen=RAM_SIZE),
}
state_lock = threading.Lock()


# ─── SQLite
def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS power (
            ts   INTEGER PRIMARY KEY,
            watt INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_power_ts ON power(ts)")
    # 月初積算値テーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kwh_baseline (
            year_month TEXT PRIMARY KEY,  -- 'YYYY-MM'
            kwh        REAL NOT NULL
        )
    """)
    conn.commit()
    return conn

db_conn = db_connect()
db_lock = threading.Lock()


def db_insert(ts: int, watt: int):
    with db_lock:
        db_conn.execute("INSERT OR REPLACE INTO power(ts,watt) VALUES(?,?)", (ts, watt))
        db_conn.commit()


def db_cleanup():
    cutoff = int((datetime.now() - timedelta(days=DB_KEEP_DAYS)).timestamp())
    with db_lock:
        db_conn.execute("DELETE FROM power WHERE ts < ?", (cutoff,))
        db_conn.commit()


def db_query(since_ts: int) -> list:
    with db_lock:
        rows = db_conn.execute(
            "SELECT ts, watt FROM power WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        ).fetchall()
    return [{"ts": r[0], "w": r[1]} for r in rows]


def db_get_baseline(ym: str) -> float | None:
    with db_lock:
        row = db_conn.execute(
            "SELECT kwh FROM kwh_baseline WHERE year_month=?", (ym,)
        ).fetchone()
    return row[0] if row else None


def db_set_baseline(ym: str, kwh: float):
    with db_lock:
        db_conn.execute(
            "INSERT OR REPLACE INTO kwh_baseline(year_month,kwh) VALUES(?,?)", (ym, kwh)
        )
        db_conn.commit()


def update_kwh_month(kwh_total: float) -> float | None:
    """当月消費量を返す。月初ベースラインがなければ登録して 0 を返す。"""
    now = datetime.now()
    ym  = now.strftime("%Y-%m")
    baseline = db_get_baseline(ym)
    if baseline is None:
        # 月が変わった瞬間（または初回）は現在値をベースラインとして記録
        db_set_baseline(ym, kwh_total)
        return 0.0
    return max(0.0, round(kwh_total - baseline, 1))


# ─── Wi-SUN 通信
def open_serial():
    return serial.Serial(
        SERIAL_PORT, baudrate=BAUD_RATE,
        bytesize=8, parity=serial.PARITY_NONE,
        stopbits=1, timeout=10,
    )


def send_cmd(ser, cmd: str):
    print(f"  >> {cmd}", file=sys.stderr)
    ser.write((cmd + "\r\n").encode())


def readline(ser) -> str:
    raw  = ser.readline()
    line = raw.decode(errors="replace").strip() if raw else ""
    if line:
        print(f"  << {line}", file=sys.stderr)
    return line


def wait_for(ser, keyword: str, timeout: float = 15.0) -> list:
    lines    = []
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

    print("[Wi-SUN] スキャン中...", file=sys.stderr)
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

    print(f"[Wi-SUN] メータ発見 ch={channel} pan={pan_id} mac={mac_addr}", file=sys.stderr)
    send_cmd(ser, f"SKSREG S2 {channel}")
    wait_for(ser, "OK")
    send_cmd(ser, f"SKSREG S3 {pan_id}")
    wait_for(ser, "OK")

    send_cmd(ser, f"SKLL64 {mac_addr}")
    lines = wait_for(ser, "FE80", timeout=5.0)
    ipv6  = next((l for l in lines if l.startswith("FE80")), None)
    if not ipv6:
        raise RuntimeError("IPv6アドレスの取得に失敗しました")

    print(f"[Wi-SUN] PANA認証中... ipv6={ipv6}", file=sys.stderr)
    send_cmd(ser, f"SKJOIN {ipv6}")
    wait_for(ser, "OK")

    deadline = time.time() + 90.0
    while time.time() < deadline:
        line = readline(ser)
        if "EVENT 25" in line:
            print("[Wi-SUN] PANA認証成功", file=sys.stderr)
            return ipv6
        if "EVENT 24" in line:
            raise RuntimeError("PANA認証失敗")
    raise TimeoutError("PANA認証タイムアウト")


def request_power(ser, ipv6: str):
    datalen = f"{len(EL_FRAME):04X}"
    header  = f"SKSENDTO 1 {ipv6} 0E1A 1 {datalen} "
    ser.write(header.encode() + EL_FRAME + b"\r\n")
    deadline = time.time() + 15.0
    while time.time() < deadline:
        line = readline(ser)
        if line.startswith("ERXUDP"):
            parts = line.split(" ")
            if len(parts) >= 9:
                return parse_el(parts[-1])
    return None, None, None, None


def parse_el(hex_str: str):
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        return None, None, None, None
    if len(data) < 12 or data[10] != 0x72:
        return None, None, None, None

    watt = ampere_r = ampere_t = kwh = None
    idx, opc = 12, data[11]
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
        elif epc == 0xE0 and pdc == 4:
            # 積算電力量（単位：0.1 kWh / スケール係数はデフォルト 0xE1=0x00 → 0.1）
            raw = int.from_bytes(data[idx:idx+4], "big", signed=False)
            kwh = round(raw * 0.1, 1)
        idx += pdc
    return watt, ampere_r, ampere_t, kwh


# ─── バックグラウンドスレッド
def wisun_worker():
    cleanup_counter = 0
    while True:
        ser = None
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
                watt, ar, at, kwh = request_power(ser, ipv6)
                now = datetime.now()
                ts  = int(now.timestamp())
                with state_lock:
                    if watt is not None:
                        state["watt"]       = watt
                        state["ampere_r"]   = ar
                        state["ampere_t"]   = at
                        state["updated_at"] = now.isoformat()
                        state["status"]     = "ok"
                        state["ram"].append({"ts": ts, "w": watt})
                    if kwh is not None:
                        state["kwh_total"] = kwh
                        state["kwh_month"] = update_kwh_month(kwh)

                if watt is not None:
                    db_insert(ts, watt)
                cleanup_counter += 1
                if cleanup_counter >= 120:
                    db_cleanup()
                    cleanup_counter = 0
                time.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"[wisun_worker エラー] {e}", file=sys.stderr)
            with state_lock:
                state["status"] = "error"
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
            time.sleep(15)


# ─── FastAPI
@asynccontextmanager
async def lifespan(application):
    t = threading.Thread(target=wisun_worker, daemon=True)
    t.start()
    print("[server] Wi-SUNワーカースレッド起動", file=sys.stderr)
    yield


app = FastAPI(lifespan=lifespan)

APPLIANCES = [
    {"name": "ドライヤー（強）",  "watt": 1200},
    {"name": "電子レンジ",        "watt": 1400},
    {"name": "エアコン（暖房）",  "watt": 1500},
    {"name": "コーヒーマシン",    "watt": 1450},
    {"name": "食洗機",            "watt": 1200},
    {"name": "電気ケトル",        "watt": 1300},
    {"name": "掃除機",            "watt":  600},
    {"name": "炊飯器",            "watt": 1450},
    {"name": "洗濯乾燥機",        "watt": 1400},
    {"name": "照明（10灯）",      "watt":  100},
]


@app.get("/api/power")
def api_power():
    with state_lock:
        watt       = state["watt"]
        ar         = state["ampere_r"]
        at_        = state["ampere_t"]
        kwh_total  = state["kwh_total"]
        kwh_month  = state["kwh_month"]
        updated_at = state["updated_at"]
        status     = state["status"]

    pct         = round(watt / MAX_WATT * 100, 1) if watt is not None else None
    remaining_w = (MAX_WATT - watt) if watt is not None else None
    ok_apps, ng_apps = [], []
    if remaining_w is not None:
        for a in APPLIANCES:
            (ok_apps if a["watt"] <= remaining_w else ng_apps).append(a)

    return {
        "watt":        watt,
        "ampere_r":    round(ar  / 10, 1) if ar  is not None else None,
        "ampere_t":    round(at_ / 10, 1) if at_ is not None else None,
        "pct":         pct,
        "max_watt":    MAX_WATT,
        "contract_a":  CONTRACT_A,
        "remaining_w": remaining_w,
        "kwh_total":   kwh_total,
        "kwh_month":   kwh_month,
        "updated_at":  updated_at,
        "status":      status,
        "ok_apps":     ok_apps,
        "ng_apps":     ng_apps,
    }


@app.get("/api/history")
def api_history(range: str = Query("1h", pattern="^(1h|24h)$")):
    now = datetime.now()
    if range == "1h":
        since_ts = int((now - timedelta(hours=1)).timestamp())
        with state_lock:
            rows = [r for r in state["ram"] if r["ts"] >= since_ts]
    else:
        since_ts = int((now - timedelta(hours=24)).timestamp())
        rows = db_query(since_ts)
    if len(rows) > 300:
        step = len(rows) // 300
        rows = rows[::step]
    return {
        "range":  range,
        "points": [
            {
                "t":  datetime.fromtimestamp(r["ts"]).strftime("%H:%M"),
                "ts": r["ts"],
                "w":  r["w"],
            }
            for r in rows
        ],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)