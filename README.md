# post-dummy-data
シナリオテストなどで用いるダミーデータ補填用Script置き場

## 使い方

### venv(推奨)
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   
```

### コンテナ(local環境以外に接続する場合は楽)
コンテナ起動と初期設定
* イメージビルド
```
$ docker compose build
```

* コンテナ起動
```
$ docker compose up
```

* コンテナ接続
```
$ docker exec -it post_dummy_data  bash
```

### .envの準備
beta/stgにPOSTしたい場合は、```.env```ファイルを用意し、basic認証の情報を入れてください
```
# .envファイル
# Authorizaiton: Basic <この部分を.envファイルに書く>
BETA_AUTH=<betaの認証情報>
STG_AUTH=<stg認証情報>
```

### ダミーデータのPOST
対象の`deveui`, `devtype`を引数で渡すとダミーデータが指定した環境へPOSTされます。
```
$ python main.py --deveui DEVEUI --devtype DEVTYPE
```
```
# 例1: local環境に1回だけbattery_lowのデータをPOST
SDMS_ENV=local
DEVEUI=14f8deae4713fc89
GWEUI=b8068af0f904e82b
DEVTYPE=tsuruga_tc7933lw1x57
STATE=battery_low
INTERVAL=0

python main.py --env $SDMS_ENV --deveui $DEVEUI --gweui $GWEUI --devtype $DEVTYPE --state $STATE --interval $INTERVAL

# 例2: stateに関係なくraw_dataを上書き

python main.py --env $SDMS_ENV --deveui $DEVEUI --gweui $GWEUI --devtype $DEVTYPE --state $STATE --interval $INTERVAL --dat
a 8c48133f89403337fa02

# 例3: fportを上書き

python main.py --env $SDMS_ENV --deveui $DEVEUI --gweui $GWEUI --devtype $DEVTYPE --state $STATE --interval $INTERVAL --dat
a 8c48133f89403337fa02 --fport 200
```

なお、引数の詳細は以下コマンドで確認できます。(下記は実行例)
```
$ python main.py -h
usage: main.py [-h] [--env {local,beta,int,stg}] --deveui DEVEUI --devtype DEVTYPE [--gweui GWEUI]
               [--state {random,battery_low,normal}] [--interval INTERVAL] [--data DATA] [--fport FPORT] [--dry-run]

POST dummy data to local/beta/int/stg

options:
  -h, --help            show this help message and exit
  --env {local,beta,int,stg}
                        target environment (local, beta, int, stg), default to int
  --deveui DEVEUI       State to device EUI
  --devtype DEVTYPE     State to device address
  --gweui GWEUI         State to gateway EUI. default to 0000000000000001
  --state {random,battery_low,normal}
                        State to determine the value type. default to normal
  --interval INTERVAL   Interval time in seconds(default to 60). To avoid looping, set inverval to zero
  --data DATA           force to overwrite raw_data regardless of state
  --fport FPORT         force to overwrite fport
  --dry-run             Prints json payload without POST
```
