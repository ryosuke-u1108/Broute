#!/usr/bin/env python3
"""
Wi-SUN Bルート 瞬時電力取得スクリプト
対象ドングル: RL7023 Stick-D/IPS (TESSERA Technology)
動作環境: Raspberry Pi + Python 3.7+

必要な設定:
  BROUTE_ID  : BルートID (電力会社から発行された32文字)
  BROUTE_PWD : Bルートパスワード (電力会社から発行された12文字)
  SERIAL_PORT: シリアルポート (例: /dev/ttyUSB0)
"""

import serial
import time
import sys
from typing import Optional

# ============================================================
# ★ 設定をここに入力してください ★
# ============================================================
BROUTE_ID = "00000099021A00000000000000D6360D"  # BルートID (32文字)
BROUTE_PWD = "LR2UJIKLM6HQ"  # Bルートパスワード (12文字)
SERIAL_PORT = "/dev/tty.usbserial-DJ007AIG"  # シリアルポート
BAUD_RATE = 115200
# ============================================================

# ECHONET Lite 定数
EL_FRAME = bytes(
    [
        0x10,
        0x81,  # EHD (ECHONET Lite)
        0x00,
        0x01,  # TID
        0x05,
        0xFF,
        0x01,  # SEOJ: コントローラ (管理・操作関連機器クラス)
        0x02,
        0x88,
        0x01,  # DEOJ: 低圧スマート電力量メータ
        0x62,  # ESV: Get要求
        0x01,  # OPC: プロパティ数
        0xE7,  # EPC: 瞬時電力計測値
        0x00,  # PDC: データなし
    ]
)


def open_serial(port: str, baudrate: int) -> serial.Serial:
    return serial.Serial(
        port,
        baudrate=baudrate,
        bytesize=8,
        parity=serial.PARITY_NONE,
        stopbits=1,
        timeout=10,
    )


def send_command(ser: serial.Serial, cmd: str) -> None:
    """SKコマンドを送信する (CRLF付き)"""
    full_cmd = cmd + "\r\n"
    ser.write(full_cmd.encode())
    print(f"  >> {cmd}")


def wait_response(
    ser: serial.Serial, expected: str, timeout: float = 15.0
) -> list[str]:
    """指定キーワードが含まれる行が来るまで待つ。収集した行を返す"""
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").strip()
        if line:
            print(f"  << {line}")
            lines.append(line)
        if expected in line:
            return lines
    raise TimeoutError(f"タイムアウト: '{expected}' が受信できませんでした")


def scan_and_join(ser: serial.Serial) -> tuple[str, str, str]:
    """
    アクティブスキャンでコーディネータを探し、PANA認証で接続する。
    戻り値: (ipv6_addr, channel, pan_id)
    """
    # --- パスワード設定 ---
    send_command(ser, f"SKSETPWD C {BROUTE_PWD}")
    wait_response(ser, "OK")

    # --- BルートID設定 ---
    send_command(ser, f"SKSETRBID {BROUTE_ID}")
    wait_response(ser, "OK")

    # --- アクティブスキャン (mode=2, 全チャンネル, duration=6) ---
    print("\n[スキャン中...]")
    send_command(ser, "SKSCAN 2 FFFFFFFF 6")
    # EVENT 0x22 (スキャン完了) を待つ
    lines = wait_response(ser, "EVENT 22", timeout=60.0)

    # EPANDESC を探してチャンネル・PAN ID・MACアドレスを取得
    channel = pan_id = mac_addr = None
    for i, line in enumerate(lines):
        if "Channel:" in line:
            channel = line.split(":")[1].strip()
        elif "Pan ID:" in line:
            pan_id = line.split(":")[1].strip()
        elif "Addr:" in line:
            mac_addr = line.split(":")[1].strip()

    if not all([channel, pan_id, mac_addr]):
        raise RuntimeError(
            "スマートメータが見つかりませんでした。BルートIDとパスワード、電波状態を確認してください。"
        )

    print(f"\n[メータ発見] Channel={channel}  PAN ID={pan_id}  MAC={mac_addr}")

    # --- チャンネルと PAN ID を設定 ---
    send_command(ser, f"SKSREG S2 {channel}")
    wait_response(ser, "OK")
    send_command(ser, f"SKSREG S3 {pan_id}")
    wait_response(ser, "OK")

    # --- MAC → IPv6 変換 ---
    send_command(ser, f"SKLL64 {mac_addr}")
    lines = wait_response(ser, "FE80", timeout=5.0)
    ipv6_addr = None
    for line in lines:
        if line.startswith("FE80"):
            ipv6_addr = line.strip()
            break
    if not ipv6_addr:
        raise RuntimeError("IPv6アドレスの取得に失敗しました")

    print(f"[IPv6] {ipv6_addr}")

    # --- PANA 接続 ---
    print("\n[PANA認証中...]")
    send_command(ser, f"SKJOIN {ipv6_addr}")
    wait_response(ser, "OK")

    # EVENT 25 (接続成功) または EVENT 24 (失敗) を待つ
    # ※ 途中で EVENT 21 など別のイベントが来ても読み続ける
    deadline = time.time() + 90.0
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").strip()
        if line:
            print(f"  << {line}")
        if "EVENT 25" in line:
            print("[PANA認証成功]")
            return ipv6_addr, channel, pan_id
        if "EVENT 24" in line:
            raise RuntimeError("PANA認証失敗。パスワードとIDを確認してください。")

    raise TimeoutError("PANA認証がタイムアウトしました")


def build_udp_sendto(ipv6: str, payload: bytes) -> tuple[str, bytes]:
    """SKSENDTO コマンド文字列を組み立てる (ポート0x0E1A=3610)"""
    datalen = f"{len(payload):04X}"
    # SKSENDTO handle ipaddr port sec datalen data
    # handle=1, port=0E1A, sec=1 (暗号化)
    header = f"SKSENDTO 1 {ipv6} 0E1A 1 {datalen} "
    return header, payload


def get_instant_power(ser: serial.Serial, ipv6: str) -> Optional[int]:
    """ECHONET Lite Get で瞬時電力(W)を取得する"""
    header, payload = build_udp_sendto(ipv6, EL_FRAME)
    # SKSENDTO はデータ部をバイナリとして直接送る
    cmd_bytes = (header).encode() + payload + b"\r\n"
    ser.write(cmd_bytes)
    print(f"  >> SKSENDTO ... (EL瞬時電力Get)")

    # ERXUDP イベントを待つ (UDP受信 = メータからの応答)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").strip()
        if line:
            print(f"  << {line}")
        if line.startswith("ERXUDP"):
            # ERXUDP SENDER DEST RPORT LPORT SENDERLLA SECURED DATALEN DATA
            parts = line.split(" ")
            if len(parts) < 9:
                continue
            data_hex = parts[-1]
            return parse_el_response(data_hex)
    return None


def parse_el_response(data_hex: str) -> Optional[int]:
    """
    ECHONET Lite レスポンスを解析して瞬時電力(W)を返す
    データはHEX ASCII文字列で受信される
    """
    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        print(f"  [警告] HEXデコード失敗: {data_hex}")
        return None

    # EL フレーム最低長チェック (EHD:2, TID:2, SEOJ:3, DEOJ:3, ESV:1, OPC:1 = 12byte)
    if len(data) < 12:
        return None

    esv = data[10]  # ESV
    opc = data[11]  # プロパティ数

    # ESV=0x72 (Get_Res) を確認
    if esv != 0x72:
        return None

    idx = 12
    for _ in range(opc):
        if idx + 2 > len(data):
            break
        epc = data[idx]
        pdc = data[idx + 1]
        idx += 2
        if epc == 0xE7 and pdc == 4:
            # 瞬時電力: signed 32bit big-endian
            watt = int.from_bytes(data[idx : idx + 4], byteorder="big", signed=True)
            return watt
        idx += pdc

    return None


def main():
    print("=" * 50)
    print("  Wi-SUN Bルート 瞬時電力取得")
    print(f"  ポート: {SERIAL_PORT}")
    print("=" * 50)

    try:
        ser = open_serial(SERIAL_PORT, BAUD_RATE)
    except serial.SerialException as e:
        print(f"[エラー] シリアルポートを開けません: {e}")
        sys.exit(1)

    try:
        # --- リセット ---
        print("\n[初期化]")
        send_command(ser, "SKRESET")
        wait_response(ser, "OK")

        # --- バージョン確認 ---
        send_command(ser, "SKVER")
        wait_response(ser, "OK")

        # --- スキャン & PANA認証 ---
        ipv6, channel, pan_id = scan_and_join(ser)

        # --- 定期的に瞬時電力を取得 ---
        print("\n[電力取得開始] Ctrl+C で終了\n")
        while True:
            watt = get_instant_power(ser, ipv6)
            if watt is not None:
                print(f"\n  ★ 瞬時電力: {watt} W\n")
            else:
                print("  (データ取得できませんでした)")
            time.sleep(10)  # 10秒ごとに取得

    except KeyboardInterrupt:
        print("\n\n終了します")
    except (TimeoutError, RuntimeError) as e:
        print(f"\n[エラー] {e}")
        sys.exit(1)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
