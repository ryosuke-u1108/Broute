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
import csv
import io
from collections import deque
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from os import getenv

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response
import uvicorn

# ============================================================
# ★ 設定 (env から読みにいく)
# ============================================================
BROUTE_ID  = getenv("BROUTE_ID", "00000099021A00000000000000D6360D")
BROUTE_PWD = getenv("BROUTE_PWD", "LR2UJIKLM6HQ")
SERIAL_PORT = getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE   = int(getenv("BAUD_RATE", "115200"))

CONTRACT_A = int(getenv("CONTRACT_A", "50"))
VOLTAGE    = int(getenv("VOLTAGE", "220"))
MAX_WATT   = int(getenv("MAX_WATT", "5000"))

POLL_INTERVAL = int(getenv("POLL_INTERVAL", "30"))
RAM_SIZE      = int(getenv("RAM_SIZE", "2880"))  # RAM_SIZE × POLL秒 = 保存期間
DB_PATH       = getenv("DB_PATH", "power.db")
DB_KEEP_DAYS  = int(getenv("DB_KEEP_DAYS", "2"))

# 電気代計算用（月度予測・リアルタイムコスト用）
COST_BASIC = float(getenv("COST_BASIC", "858"))   # 基本料金（円/月）
COST_R1    = float(getenv("COST_R1", "29.90"))     # 第1段階（円/kWh）
COST_R2    = float(getenv("COST_R2", "35.59"))     # 第2段階（円/kWh）
COST_R3    = float(getenv("COST_R3", "36.50"))     # 第3段階（円/kWh）
COST_ADJ   = float(getenv("COST_ADJ", "0"))        # 燃料調整等（円/kWh）
# ============================================================

# ECHONET Lite フレーム（瞬時電力 0xE7 + 瞬時電流 0xE8 + 積算電力量 0xE0 + 係数 0xE1）
EL_FRAME = bytes([
    0x10, 0x81, 0x00, 0x01,
    0x05, 0xFF, 0x01,
    0x02, 0x88, 0x01,
    0x62,        # GET
    0x04,        # OPC: 4プロパティ
    0xE7, 0x00,  # 瞬時電力
    0xE8, 0x00,  # 瞬時電流
    0xE0, 0x00,  # 積算電力量（正方向）
    0xE1, 0x00,  # 積算電力量の係数
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
        line = readline(ser)  # serial timeout=10 でブロックするので過負荷にはならないが念のため
        if line.startswith("ERXUDP"):
            parts = line.split(" ")
            if len(parts) >= 9:
                return parse_el(parts[-1])
        elif not line:
            # readline がタイムアウトで空文字を返した場合は少し待つ
            time.sleep(0.1)
    return None, None, None, None


# EPC 0xE1 で得られるスケール係数テーブル (ECHONET Lite v1.10)
# EPC値 -> 係数 (kWh単位)
# 0x00 = 0.1, 0x80 = 1.0, 0x81 = 10.0, 0x82 = 0.01, 0x83 = 100.0
E1_COEFFICIENTS = {
    0x00: 0.1,
    0x80: 1.0,
    0x81: 10.0,
    0x82: 0.01,
    0x83: 100.0,
}


def parse_el(hex_str: str):
    """ECHONET Lite 応答をパース。0xE1 係数にも対応。"""
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        return None, None, None, None
    if len(data) < 12 or data[10] != 0x72:
        return None, None, None, None

    watt = ampere_r = ampere_t = kwh = None
    coef = 0.1  # デフォルト係数
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
        elif epc == 0xE1 and pdc >= 1:
            # 係数は 1 バイトで表現
            coef = E1_COEFFICIENTS.get(data[idx], 0.1)
        elif epc == 0xE0 and pdc >= 4:
            raw = int.from_bytes(data[idx:idx+4], "big", signed=False)
            kwh = round(raw * coef, 1)
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

                # update_kwh_month は例外で inner loop が落ちると
                # Wi-SUN 再接続(60s+)で表示が完全に止まるため
                # 例外を捕まえてGraceful fallback する
                kwh_month_new = None
                if kwh is not None:
                    try:
                        kwh_month_new = update_kwh_month(kwh)
                    except Exception:
                        print("[wisun_worker] update_kwh_month failed (keeping old value)", file=sys.stderr)

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
                        state["kwh_month"] = kwh_month_new

                if watt is not None:
                    try:
                        db_insert(ts, watt)
                    except Exception:
                        pass
                cleanup_counter += 1
                if cleanup_counter >= 120:
                    try:
                        db_cleanup()
                    except Exception:
                        pass
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


def _suggest_appliance(delta_w: int) -> str | None:
    """増加分に近い家電を推測して返す"""
    if delta_w < 50:
        return "照明の付け替え"
    close = min(APPLIANCES, key=lambda a: abs(a["watt"] - delta_w))
    if abs(close["watt"] - delta_w) < 300:
        return f"おそらく{close['name']}"
    if delta_w < 200:
        return "照明や小型家電の可能性"
    return None


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

    # ─── リアルタイム電気代（秒間コスト）
    live_cost_per_sec = None
    if watt is not None and kwh_month is not None:
        basic  = COST_BASIC
        r1     = COST_R1
        r2     = COST_R2
        r3     = COST_R3
        adj    = COST_ADJ
        # 瞬間電力(W) → 瞬間消費(kWh/秒)
        instant_kwh_sec = watt / 3600000.0
        # 単価計算
        if kwh_month <= 120:
            rate = r1 + adj
        elif kwh_month <= 300:
            rate = r2 + adj
        else:
            rate = r3 + adj
        live_cost_per_sec = round(instant_kwh_sec * rate, 6)

    # ─── 電力スパイク検知
    spike_detected = False
    spike_info     = None
    if watt is not None and len(state["ram"]) >= 4:
        recent = [r["w"] for r in list(state["ram"])[-4:]]
        if len(recent) >= 4:
            old_vals = recent[:3]
            newest = recent[3]
            avg_old = sum(old_vals) / len(old_vals)
            if avg_old > 100:  # 100W以上が基準
                increase = (newest - avg_old) / avg_old
                if increase >= 0.4:  # 40%以上上昇
                    spike_detected = True
                    spike_info = {
                        "watt":      newest,
                        "increase":  round(increase * 100, 1),
                        "previous":  round(avg_old, 0),
                        "appliance": _suggest_appliance(newest - avg_old),
                    }

    # ─── 当月コスト予測
    monthly_prediction = None
    if kwh_month is not None and watt is not None:
        now      = datetime.now()
        days_in_month = (now.replace(month=now.month+1 if now.month < 12 else 1, year=now.year+1 if now.month == 12 else now.year, day=1) - now.replace(day=1)).days
        day_of_month = now.day
        hours_remaining = (now.replace(day=1, month=now.month+1 if now.month < 12 else 1, year=now.year+1 if now.month == 12 else now.year) - now).total_seconds() / 3600
        current_rate_w = watt  # 現在の瞬間電力(W)
        # 現在値を瞬間電力で推計（＝今の消費ペースで残り時間でどれくらい使うか）
        predicted_remaining_kwh = current_rate_w * hours_remaining / 3600000
        predicted_month_kwh = round(kwh_month + predicted_remaining_kwh, 1)

        # 過去1日の平均消費ペースでも補正
        yesterday = now - timedelta(days=1)
        try:
            rows_24h = db_query(int(yesterday.timestamp()))
            if rows_24h:
                avg_w_24 = sum(r["w"] for r in rows_24h) / len(rows_24h)
                predicted_24h = avg_w_24 * hours_remaining / 3600000
                predicted_month_24h = round(kwh_month + predicted_24h, 1)
            else:
                predicted_month_24h = predicted_month_kwh
        except Exception:
            predicted_month_24h = predicted_month_kwh

        basic  = COST_BASIC
        r1     = COST_R1
        r2     = COST_R2
        r3     = COST_R3
        adj    = COST_ADJ

        def calc_cost(kwh):
            if kwh <= 120:
                return round(kwh * (r1 + adj)) + round(basic)
            elif kwh <= 300:
                return round(120 * (r1 + adj) + (kwh - 120) * (r2 + adj)) + round(basic)
            else:
                return round(120 * (r1 + adj) + 180 * (r2 + adj) + (kwh - 300) * (r3 + adj)) + round(basic)

        cost_now     = calc_cost(predicted_month_kwh)
        cost_24h     = calc_cost(predicted_month_24h)
        prev_month = now.month - 1 if now.month > 1 else 12
        prev_year  = now.year if now.month > 1 else now.year - 1
        prev_ym    = f"{prev_year:04d}-{prev_month:02d}"
        prev_kwh   = db_get_baseline(prev_ym)
        prev_cost  = calc_cost(prev_kwh) if prev_kwh is not None else None

        if prev_cost is not None and prev_cost > 0:
            diff_pct = round((cost_now - prev_cost) / prev_cost * 100, 1)
            if diff_pct > 0:
                diff_note = f"+{diff_pct:.1f}%"
            elif diff_pct < 0:
                diff_note = f"{diff_pct:.1f}%"
            else:
                diff_note = "±0%"
        else:
            diff_note = "—"

        monthly_prediction = {
            "predicted_kwh": predicted_month_kwh,
            "predicted_kwh_24h": predicted_month_24h,
            "cost_now": cost_now,
            "cost_24h": cost_24h,
            "prev_cost": prev_cost,
            "diff_pct": diff_note,
        }

    return {
        "watt":             watt,
        "ampere_r":         round(ar  / 10, 1) if ar  is not None else None,
        "ampere_t":         round(at_ / 10, 1) if at_ is not None else None,
        "pct":              pct,
        "max_watt":         MAX_WATT,
        "contract_a":       CONTRACT_A,
        "remaining_w":      remaining_w,
        "kwh_total":        kwh_total,
        "kwh_month":        kwh_month,
        "updated_at":       updated_at,
        "status":           status,
        "ok_apps":          ok_apps,
        "ng_apps":          ng_apps,
        "live_cost_per_sec": live_cost_per_sec,
        "spike_detected":   spike_detected,
        "spike_info":       spike_info,
        "monthly_prediction": monthly_prediction,
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


@app.get("/api/export")
def api_export(
    range: str = Query("1h", pattern="^(1h|24h)$"),
    format: str = Query("csv", pattern="^(csv|json)$"),
):
    now = datetime.now()
    if range == "1h":
        since_ts = int((now - timedelta(hours=1)).timestamp())
        with state_lock:
            rows = [r for r in state["ram"] if r["ts"] >= since_ts]
    else:
        since_ts = int((now - timedelta(hours=24)).timestamp())
        rows = db_query(since_ts)

    if format == "json":
        return {
            "range": range,
            "points": [
                {
                    "t": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "ts": r["ts"],
                    "w": r["w"],
                }
                for r in rows
            ],
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "time", "watt"])
    for r in rows:
        writer.writerow([
            datetime.fromtimestamp(r["ts"]).isoformat(),
            datetime.fromtimestamp(r["ts"]).strftime("%H:%M"),
            r["w"],
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=power_{range}_{now.strftime('%Y%m%d_%H%M')}.csv"},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)