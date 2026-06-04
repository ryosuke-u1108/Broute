# Wi-SUN Bルート 電力モニター

ラズパイ + Wi-SUN アダプターでスマートメータからリアルタイムに電力データを取得し、ブラウザで確認できるホームモニターです。

---

## 画面でできること

- 瞬時電力（W）・R相/T相電流（A）のリアルタイム表示
- 契約容量に対する負荷率ゲージ（ブレーカー危険度の可視化）
- 1時間・24時間の電力推移チャート
- 当月の積算電力量（kWh）
- 3段階従量課金に対応した電気代概算
- 残り使用可能電力と、追加で動かせる家電の一覧

---

## 必要なもの

| 項目 | 備考 |
|------|------|
| Raspberry Pi（任意のモデル） | OS: Raspberry Pi OS / Ubuntu など |
| Wi-SUN USB アダプター | BP35A1 / RL7023 Stick-D/IPS など |
| Bルート認証ID・パスワード | 電力会社に申請して取得 |
| Python 3.10 以上 | |

---

## セットアップ

### 1. リポジトリを配置

```bash
mkdir ~/power-monitor && cd ~/power-monitor
# server.py と index.html をこのディレクトリに置く
```

### 2. 依存パッケージをインストール

```bash
pip install fastapi uvicorn pyserial --break-system-packages
```

### 3. 設定を編集

`server.py` の先頭にある設定ブロックを書き換えます。

```python
BROUTE_ID   = "YOUR_BROUTE_ID"       # Bルート認証ID（32桁）
BROUTE_PWD  = "YOUR_BROUTE_PASSWORD" # Bルート認証パスワード
SERIAL_PORT = "/dev/ttyUSB0"         # Wi-SUN アダプターのポート
MAX_WATT    = 5000                   # 契約アンペア × 100（50A契約なら5000）
```

ポートが不明な場合は `ls /dev/ttyUSB*` または `ls /dev/ttyACM*` で確認してください。

### 4. 起動

```bash
python server.py
```

ブラウザで `http://ラズパイのIPアドレス:8000` を開きます。

初回起動時は Wi-SUN スキャン・PANA 認証に最大1〜2分かかります。画面に「CONNECTING」と表示されている間はそのままお待ちください。

---

## 自動起動（systemd）

```ini
# /etc/systemd/system/power-monitor.service

[Unit]
Description=Power Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/power-monitor/server.py
WorkingDirectory=/home/pi/power-monitor
Restart=always
RestartSec=15
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable power-monitor
sudo systemctl start power-monitor
```

---

## 電気代の設定

画面右下の「⚙ 料金設定」カードから入力します。設定値はブラウザの localStorage に保存されるため、ページを閉じても保持されます。

| 項目 | 説明 |
|------|------|
| 基本料金 | 月固定料金（円/月） |
| 〜120kWh | 第1段階単価（円/kWh） |
| 121〜300kWh | 第2段階単価（円/kWh） |
| 301kWh〜 | 第3段階単価（円/kWh） |
| 燃料調整等 | 燃料費調整額・再エネ賦課金などの合算（円/kWh、マイナス可） |

計算式:

```
電気代 = 基本料金
       + 120kWh × 第1段階
       + (使用量 - 120) × 第2段階    ← 121kWh以上の場合
       + (使用量 - 300) × 第3段階    ← 301kWh以上の場合
       ※ 各段階に燃料調整額を加算
```

---

## API

| エンドポイント | 説明 |
|----------------|------|
| `GET /api/power` | 最新の電力データ（JSON） |
| `GET /api/history?range=1h` | 直近1時間の推移データ |
| `GET /api/history?range=24h` | 直近24時間の推移データ |

### `/api/power` レスポンス例

```json
{
  "watt": 312,
  "ampere_r": 7.2,
  "ampere_t": 6.8,
  "pct": 6.2,
  "max_watt": 5000,
  "remaining_w": 4688,
  "kwh_total": 1842.3,
  "kwh_month": 47.1,
  "updated_at": "2025-06-04T14:32:10",
  "status": "ok",
  "ok_apps": [...],
  "ng_apps": [...]
}
```

---

## データの保存

| 種別 | 場所 | 保存期間 |
|------|------|----------|
| 直近1日分の電力 | メモリ（deque） | 再起動でリセット |
| 過去2日分の電力 | `power.db`（SQLite） | 2日間、自動削除 |
| 月初の積算電力ベースライン | `power.db` | 永続（月ごとに1行） |

---

## 取得しているECHONET Liteプロパティ

| EPC | 内容 | 更新頻度 |
|-----|------|----------|
| `0xE7` | 瞬時電力計測値（W） | 30秒ごと |
| `0xE8` | 瞬時電流計測値（R相・T相） | 30秒ごと |
| `0xE0` | 積算電力量（正方向、0.1kWh単位） | メータ依存（多くは30分ごと更新） |

---

## トラブルシューティング

**CONNECTING のまま進まない**
Wi-SUN アダプターのポート名を確認してください。またスマートメータとの距離が遠すぎると接続に失敗することがあります。`server.py` の標準エラー出力にログが出るので `journalctl -u power-monitor -f` で確認できます。

**積算電力量が0のまま**
メータによっては `0xE0` の係数（`0xE1` プロパティ）が 0.1kWh 以外の場合があります。その場合は `parse_el` 内の係数 `0.1` を調整するか、`0xE1` も取得して動的に切り替える対応が必要です。

**電気代の計算が合わない**
燃料費調整額や再エネ賦課金は月ごとに変わります。請求書と照らし合わせて「燃料調整等」の欄を最新の値に更新してください。
