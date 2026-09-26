# XW-4（愛称: 門番くん）

玄関カメラで来客を検知・顔認識し、訪問日時とともに記録・統計・通知するローカル Web サービス。

命名規則: X=実験段階, W=Webサービス, 4=カテゴリ内連番

## 構成

```mermaid
flowchart LR
    CAM["玄関カメラ Victure PC530<br/>192.168.11.27<br/>RTSP :554 (H.264 1080p)"]

    subgraph DOCKER["Docker（compose）"]
        direction TB
        subgraph FRIGATE["frigate コンテナ"]
            direction TB
            G2["go2rtc<br/>カメラに1本だけ接続し内部で再配信"]
            FF["ffmpeg デコード<br/>1280x720 / 5fps"]
            DET["物体検知 OpenVINO<br/>Mac: CPU / N100: iGPU<br/>person だけ追跡"]
            FACE["顔認識 + 見た目の埋め込み<br/>(face_recognition / semantic_search)"]
            FSTORE[("frigate.db / media/<br/>スナップショット・録画・顔ライブラリ")]
            FUI["Frigate UI + API<br/>:8971 (認証) / :5000 (内部)"]
            G2 --> FF --> DET --> FACE --> FSTORE
            FUI --- FSTORE
        end
        subgraph GK["gatekeeper コンテナ（自前）"]
            direction TB
            LOOP["15 秒ごとの処理ループ<br/>顔品質チェック → ID 自動発行<br/>見た目による紐付け / 性別推定"]
            GDB[("gatekeeper.db<br/>人物・訪問・別名")]
            WEB["統計ダッシュボード + API<br/>:8080 (内部)"]
            LOOP --> GDB --- WEB
        end
        PROXY["nginx リバースプロキシ<br/>:8080（唯一の入口）"]
        LOOP -->|"API :5000<br/>events / faces / search"| FUI
        PROXY -->|"/"| WEB
        PROXY -->|"/frigate/"| FUI
    end

    CAM -->|"RTSP"| G2
    BROWSER["ブラウザ"] -->|"http://host:8080/"| PROXY
    ALERT["見知らぬ人アラート（今後）"] -.->|"Webhook"| DISCORD["Discord"]
    GDB -.-> ALERT
```

実線が現在の流れ、点線は未実装。

- **go2rtc**: Frigate に同梱されたストリーム中継サーバー。カメラへの RTSP 接続を 1 本だけ張り、
  コンテナ内で検知用・録画用に配り直す（カメラの同時接続数を節約する）

- **Frigate 0.18**（`stable` タグは 2026-09 時点で 0.18.0）: 人物検知、ゾーン判定（玄関前 `porch`）、スナップショット、顔認識
- **本番ホスト**: N100 PC（Intel iGPU）+ Docker。OpenVINO GPU 推論 + VAAPI デコード
- **開発・試験**: Apple Silicon Mac の Docker Desktop で動作確認済み（OpenVINO CPU 推論、ソフトウェアデコード）

## カメラ

- Victure PC530（首振り可）。ONVIF の機器情報では Manufacturer `IPC365` / Model `81XXF` / Firmware `V3.15.73`
- IP 192.168.11.27（DHCP。ルーターで固定割当推奨）。アプリで ONVIF を ON にすると RTSP（554）が開く
- 開いているポート: 554（RTSP）、8080（ONVIF。PTZ 設定なし）、34567（アプリ専用プロトコル）
- 既知の問題: 1〜2 日に一度、首位置が初期位置に戻る（原因未特定）。ゾーンを使わない設計にしてあるので分類には影響しない

## 入口とポート

入口は **`http://<ホストIP>:8080/`** の 1 つ（nginx のリバースプロキシ）。

| パス | 中身 | 認証 |
| --- | --- | --- |
| `/` | 統計ダッシュボード（gatekeeper） | なし（LAN 内のみ。変更系 API は CSRF 対策のヘッダ必須） |
| `/frigate/` | Frigate UI（ライブ映像、イベント、顔ライブラリ、設定） | あり（Frigate のログイン） |

Frigate UI はサブパスで配信するため、プロキシが `X-Ingress-Path: /frigate` を付けて Frigate 側にリンクを書き換えさせている。

内部ポート（通常は触らない）:

| ポート | 何か | 公開 |
| --- | --- | --- |
| 5000 | Frigate API（認証なし・管理者権限）。gatekeeper が compose 内ネットワークで使う | ホストのループバック（127.0.0.1）のみ。curl でのデバッグ用 |
| 8971 | Frigate UI（HTTPS、自己署名） | 非公開。プロキシが内部で使う。直接開きたければ compose で `"8971:8971"` を足す |
| 1984 | go2rtc の管理画面 | 非公開。必要なら `"127.0.0.1:1984:1984"` |
| 8554 / 8555 | go2rtc RTSP 再配信 / WebRTC | 非公開。VLC などで見たければ `"8554:8554"` |

Docker Desktop のポート一覧に出るのは 8080 と 127.0.0.1:5000 だけ。
Frigate のロゴ（燕のような鳥）はグンカンドリ（frigatebird）。

## ファイル構成

| ファイル | 用途 |
| --- | --- |
| `docker-compose.yml` | ベース構成（GPU なしでも動く） |
| `docker-compose.n100.yml` | N100 用 override（`/dev/dri` を渡す） |
| `config/config.yml` | Frigate 設定・N100 版（OpenVINO GPU + VAAPI）。**これが正** |
| `config/config.mac.yml` | Frigate 設定・Mac/WSL 版（OpenVINO CPU 検出器、hwaccel なし）。config.yml との差分は検出器の `device` と hwaccel だけ |
| `.env.example` | 環境変数の雛形 |
| `services/gatekeeper/` | 自動分類サービス（後述） |
| `services/proxy/nginx.conf` | 入口を 1 つにまとめるリバースプロキシの設定 |
| `data/` | 自前 DB（`gatekeeper.db`）と顔画像のコピー。Git 管理外 |

`config/` 配下は設定ファイル 2 つ以外（DB、鍵、モデルキャッシュ、顔画像）を Git 管理外にしている。

## セットアップ

1. カメラアプリで **ONVIF を ON** にする（RTSP 554 が開く）
2. ルーターでカメラの IP を固定割当にする（現状 192.168.11.27）
3. 認証情報と実行環境を設定する

   ```bash
   cp .env.example .env
   ```

   `.env` の `RTSP_USER` / `RTSP_PASS` / `CAMERA_IP` を実際の値に書き換え（カメラの初期パスワードは変更を推奨）、
   実行環境に合わせて `FRIGATE_TAG` / `COMPOSE_FILE` / `CONFIG_FILE` のコメントを外す

   | 環境 | FRIGATE_TAG | COMPOSE_FILE | CONFIG_FILE |
   | --- | --- | --- | --- |
   | N100（amd64 + iGPU） | `stable` | `docker-compose.yml:docker-compose.n100.yml` | `/config/config.yml` |
   | Apple Silicon Mac | `stable-standard-arm64` | （未設定） | `/config/config.mac.yml` |
   | WSL など amd64・GPU なし | `stable` | （未設定） | `/config/config.mac.yml` |

   `stable` の arm64 側は Raspberry Pi 向けビルドなので、Mac では公式推奨の `stable-standard-arm64` を使う。
   タイムゾーンは `TZ` 環境変数で渡す（`/etc/localtime` のマウントは不要で、Mac では壊れやすい）

4. 起動

   ```bash
   docker compose up -d
   ```

5. 初回起動時に管理者パスワードがログに出る（`admin` ユーザー）

   ```bash
   docker compose logs frigate | grep 'Password:'
   ```

   コンテナを作り直すとこのログは消える。パスワードを控え忘れたら、設定ファイルに

   ```yaml
   auth:
     reset_admin_password: true
   ```

   を足して `docker compose restart frigate` すると新しいパスワードがログに出る。取得後はこの 2 行を削除する

6. ブラウザで **`http://<ホストIP>:8080/`** を開く。統計ページの右上「Frigate を開く」から Frigate UI（`/frigate/`）に入れる

## 動作確認コマンド

```bash
# カメラ取得 fps / 検出器の推論時間
curl -s http://localhost:5000/api/stats | python3 -m json.tool | grep -E 'camera_fps|inference_speed|detection_fps'
```

```bash
# 現在のフレームを保存（画角・顔サイズの確認用）
curl -s -o latest.jpg "http://localhost:5000/api/entrance/latest.jpg?h=720"
```

```bash
# go2rtc のストリーム状態
curl -s http://localhost:1984/api/streams | python3 -m json.tool | head -40
```

各ポートの役割は「ポート一覧」を参照。

## iGPU が使えない環境の補足（WSL）

WSL2 で `/dev/dri` が見えない場合も Mac と同じく `CONFIG_FILE=/config/config.mac.yml` を使う。
WSL2 NAT 下では WebRTC が不安定なので、Frigate UI のライブ再生は MSE を使う。

## 自動分類サービス（gatekeeper）

Frigate 自体は「人が顔ライブラリで名前を登録する」前提で、未登録の顔を勝手にまとめる機能はない。
そこで `gatekeeper` コンテナが次を自動で行う。ゾーンは使わず、映った person は通行人も含めて全員が対象。

1. Frigate の終了済み person イベントを 15 秒ごとに取得
2. イベントごとに Frigate が保存した顔の試行画像（`train`）を集め、次の順で「誰か」を決める
   - Frigate が既存 ID と認識していればそれを採用
   - 試行画像の多数決（スコア 0.8 以上）で既存 ID に一致すれば採用
   - 誰にも似ていなければ新しい ID（`p0001`, `p0002`, …）を作り、大きい顔から最大 5 枚を Frigate に登録
   - 試行画像が無いイベント（登録ゼロの時期など）はスナップショットをアップロードして照合・登録
   - **独自の顔品質チェック**（Frigate と同じ YuNet モデルを検出スコア 0.8 で使い、目の間隔と鼻の位置が妥当で、
     短辺 36px 以上の顔があるか）を、新しい ID を作る前と、Frigate の照合結果を採用する前の両方で通す。
     Frigate は検出しきい値が緩く、後頭部や頬の断片を「顔」として登録・照合してしまい、後ろ姿同士が
     0.95 以上で一致する事故が起きたため。品質を満たす顔が無い照合結果は「未特定（unverified）」として記録する
3. 決めた ID を Frigate のイベントにも書き戻す（Frigate UI でも `p0001` と表示される）
4. 訪問記録を `data/gatekeeper.db`（SQLite）に保存。顔画像のコピーは `data/faces/`
5. 使い終わった試行画像は 1 周期に 1 回まとめて削除して `train` を掃除する（Frigate は削除のたびに分類器を作り直すため）
6. `data/faces/` の顔画像コピーは `RETAIN_DAYS`（既定 90 日）で自動削除。訪問記録（人物 ID と日時）は残す

安全策:

- 1 イベントの失敗は周期全体を止めない。失敗・保留・進行中のイベントは ID で控えて毎周期確認し、`RETRY_LIMIT` 回で諦めて未特定として記録する
- カーソルは処理済みの位置だけを表し、`OVERLAP_SECONDS`（既定 120 秒）重ねて取り直すので、周期の合間の短いイベントも拾う
- Frigate 側を変更する直前に「処理中」行を書き、途中で落ちても同じ ID で続きから再開する。ID 番号は登録が成功したときだけ確定する
- `merge` した ID は別名として記憶し、古いラベルが残っていても別人を作らない。統合で消えた番号は再利用しない
- Frigate の顔ライブラリは、一度しか来ていない自動 ID を `RETAIN_DAYS` で削除し、`MAX_PERSONS`（既定 500）で頭打ちにする

登録済みの ID は Frigate 自身が次回から認識する（加重平均 0.93 以上・2 フレーム以上の一致で即時ラベル付け）。
同じ人に複数の ID が付いてしまったら `merge` で統合できる。

**補強学習はしない**（`REINFORCE_PER_EVENT=0`）。認識のたびに画像を追加していたところ、誤認識した通行人の顔まで
取り込んで「誰でも 0.95 で一致する」状態になった事故があったため。ID は作成時の品質チェック済み画像だけで構成する。
登録画像を品質チェックにかけ直して掃除するには:

```bash
docker compose exec gatekeeper python -m gatekeeper clean --dry-run
```

```bash
docker compose exec gatekeeper python -m gatekeeper clean
```

```bash
docker compose exec gatekeeper python -m gatekeeper persons
```

```bash
docker compose exec gatekeeper python -m gatekeeper visits --limit 30
```

```bash
docker compose exec gatekeeper python -m gatekeeper rename p0001 "田中さん"
```

```bash
docker compose exec gatekeeper python -m gatekeeper merge p0002 p0001
```

誤登録（顔が写っていない等）の ID を消す。Frigate の登録画像も消し、その ID の訪問は未特定に戻る。番号は再利用しない。

```bash
docker compose exec gatekeeper python -m gatekeeper purge p0005
```

表示名は自前 DB だけに持ち、Frigate 側の ID は変えない（Frigate の名前は英数字のまま安定させる）。
調整できる値は `services/gatekeeper/gatekeeper/config.py` の `Settings` を参照（環境変数で上書き可）。

テスト（GitHub Actions でも push / PR ごとに自動実行。`.github/workflows/test.yml`）:

```bash
docker compose run --rm --no-deps gatekeeper python -m pytest -q tests
```

Frigate 側の挙動で注意する点:

- 顔ライブラリが空の間は Frigate が試行画像を一切保存しない。最初の 1 人はスナップショット経由で登録される
- 立ち止まっている人でも検知が途切れると 1 回の滞在が複数イベントに分かれる（5 分の滞在が 20〜30 秒 × 5 件になった例あり）。
  人物 ID は同じになるので統計側で「同一人物・間隔 N 秒以内」を 1 回の訪問にまとめる予定

## 統計ダッシュボード

**`http://<ホストIP>:8080/`**（プロキシ経由。認証なし。LAN 内のみ）。60 秒ごとに自動更新。

- 今日の検知 / 来訪、期間内の来訪数、特定できた人物数、推定男女比
- 日別の通行量（7 / 30 / 90 日）、時間帯別（今日）
- 曜日 × 時間帯ヒートマップ（直近 28 日。全員 / 特定できた人のみ）
- 人物一覧: 顔サムネイル、表示名の編集（入力して Enter）、来訪回数、最終訪問、推定性別・年齢、統合 / 削除ボタン
- 同じ見た目の未特定グループ: 顔は見えないが服装・持ち物が似ている来訪を 24 時間以内でまとめたもの（後述）
- 最近の来訪: 画像、日時、滞在時間、人物、判定方法、見た目の推定、推定性別、**動画**（ページ内で再生。複数イベントの来訪は切替可）と Frigate の詳細画面へのリンク
- 人物一覧・見た目グループの ID をクリックすると、その人物 / グループの来訪だけに絞り込める（「解除」で戻る）

用語: **検知** = Frigate の person イベント 1 件。**来訪** = 同一人物の連続イベント（`VISIT_GAP_SECONDS`、既定 120 秒以内）を 1 回にまとめたもの。
立ち止まっている人でも検知は数十秒ごとに途切れるので、統計は来訪で見る。

性別・年齢は InsightFace の genderage モデル（非商用研究ライセンス）による**推定**で、顔品質チェックを通った顔だけに付く。
夜間の赤外線（モノクロ）映像では精度が出ないため推定しない。信頼度 0.75 未満も「不明」扱い。
人物ごとの性別は、その人物の訪問での多数決。既存の記録に付け直すには:

```bash
docker compose exec gatekeeper python -m gatekeeper backfill-gender
```

### 見た目（服装・持ち物）による紐付け

顔で特定できなかった来訪について、Frigate のセマンティック検索（`semantic_search`、Jina CLIP v1 の小型モデルで
サムネイル全体を数値化）を使い、前後 24 時間以内の来訪と見た目を比べる。

- 似ている相手が顔で特定済みなら「見た目で p0001 と推定」（`appearance_person`）。統計では本人の来訪として数えるが、画面では「推定」と表示
- 相手も未特定なら「同じ見た目のグループ」（`a0001`…）。張り紙をしに何度も来る人物のように、顔が写らないまま繰り返す来訪はここに現れる
- 顔の学習には一切使わない。人物を統合・削除すると推定も追従する
- 距離のしきい値は `APPEARANCE_MAX_DISTANCE`（人物への推定、既定 0.11）と `APPEARANCE_GROUP_MAX_DISTANCE`（未特定同士のグループ化、既定 0.09）。
  実測では同じ人の連続イベントが 0.07〜0.11、別人は概ね 0.15 以上。
  ただし埋め込みは「傘」「自転車」「リュック」といった場面の型に強く反応し、雨の日は傘の別人同士が 0.10〜0.12 まで近づく。
  そのため (1) 人物への推定は顔で特定した来訪との直接一致に限る（推定同士の連鎖はしない）、
  (2) 未特定グループへの参加はグループの起点の来訪とも似ていることを要求する（A≈B、B≈C で A と C を同一視しない）。
  それでも見た目の推定は参考値として扱い、確実なのは「同じ人が数分以内に続けて映った」ケース
- しきい値を変えたら `docker compose exec gatekeeper python -m gatekeeper relink --hours 48` で紐付けをやり直せる
- Frigate 側で `semantic_search.enabled: true` が必要（初回に約 370MB のモデルを取得）。既存イベントを索引したいときは `reindex: true` を一時的に付けて起動する

API（JSON）: `/api/summary?days=7`、`/api/traffic?days=7`、`/api/heatmap?days=28`、`/api/persons`、`/api/appearance-groups?days=7`、`/api/visits?limit=50&person=p0001`（`person` には見た目グループ `a0001` も指定可）、
`/api/events/{id}/snapshot.jpg`、`/api/events/{id}/clip.mp4`（いずれも gatekeeper が記録したイベントのみ Frigate から中継）、
`PUT /api/persons/{id}`（`{"display_name": "..."}`）、`POST /api/persons/{id}/merge`（`{"into": "p0001"}`）、`POST /api/persons/{id}/purge`

変更系（PUT / POST）は CSRF 対策として `Content-Type: application/json` と `X-Requested-With: gatekeeper` ヘッダが必須
（ブラウザのフォームからは送れない）。表示名に `< > & " '` と制御文字は使えない。集計の時刻はサーバー（コンテナ）の
タイムゾーン基準で、画面もそれに合わせて表示する。`days` は 1〜366、`limit` は 1〜500。

```bash
curl -X PUT -H 'Content-Type: application/json' -H 'X-Requested-With: gatekeeper' -d '{"display_name":"田中さん"}' http://localhost:8080/api/persons/p0001
```

## 危険度アラート（Discord）

表情ではなく**行動**で危険度を付ける。対象は「来訪」（同一人物の連続イベントをまとめたもの）で、表示名を付けた人物（家族・知人）は対象外。

| 加点 | 条件 |
| --- | --- |
| +1 | 顔を特定できない（未登録） / 名前のない自動 ID |
| +1 / +2 / +3 | 滞在 30 秒 / 90 秒 / 180 秒以上（進行中の来訪は現在時刻まで） |
| +1 | 夜間（22〜5 時） |
| +1 / +2 | 24 時間以内の再来訪が 2 回 / 4 回以上（顔または見た目のグループで判定） |
| +1 | 表情が怒り・恐れ寄り（確信度 0.6 以上。未実装、加点枠のみ） |

レベル: 5 以上「高」、3 以上「中」。`ALERT_MIN_LEVEL`（既定 medium）以上で Discord に画像付きで通知する。
進行中の滞在でも閾値を超えた時点で通知し、レベルが上がれば更新を送る。同じ人物・グループは `ALERT_COOLDOWN_SECONDS`（既定 600 秒）の間は再通知せず、
1 時間の上限は `ALERT_MAX_PER_HOUR`（既定 20）。判定結果は通知の有無にかかわらずダッシュボードの「アラート」に出る。

設定は `.env` の `DISCORD_WEBHOOK_URL`（Discord のチャンネル設定 → 連携サービス → ウェブフック）と `DASHBOARD_URL`（通知に載せるリンク）。

## 段階的な立ち上げ手順

1. 人物検知＋ゾーンを動かす。Frigate UI のゾーンエディタで `porch` の座標を実画角に合わせる
   - 現状の画角は歩道・道路を広く映し、玄関ドアは左端。仮置きの `porch`（画面中央）は歩道の通行人を拾うので要調整
   - UI で保存した座標は起動中の設定ファイル（Mac なら config.mac.yml）に書かれる。config.yml にも手で反映する
2. スナップショットで顔のサイズ（画素数）を確認する。小さければカメラの設置位置・画角を調整
3. 顔認識は有効化済み（`face_recognition.enabled: true`）
   - `model_size: small` は CPU で動く（FaceNet）。`large` は GPU が必要
   - 初回起動時にモデル（facedet.onnx / facenet.tflite 等、約 150MB）を `config/model_cache/` にダウンロードする
   - 0.17 以降、顔認識には AVX/AVX2 対応 CPU が必要と明記されている（N100 は対応。Apple Silicon Mac でも起動と推論は確認済み）
   - しきい値は既定より厳しくしてある（`unknown_score: 0.85`、`recognition_threshold: 0.93`、`min_faces: 2`）。
     小さい顔では別人にも高スコアが出るため、既定値では通行人を登録者と誤認した
4. Frigate UI の **顔ライブラリ（Face Library）** で家族の顔を登録する
   - 有効化後に `person` が検知されると、顔の切り抜きが「トレーニング（Train）」タブに溜まる（`save_attempts` 件まで）
   - その中から本人の顔を選び、名前を付けて登録する。角度・明るさの違うものを 10〜20 枚ほど登録すると安定する
   - 登録済みの人はイベントの `sub_label` に名前が入る。未登録は `unknown`
   - 通り過ぎるだけだとブレ・横顔で認識できない。玄関前でカメラの方を向いた瞬間が必要

## RTSP 直接確認

```bash
ffprobe -rtsp_transport tcp "rtsp://USER:PASS@192.168.11.27:554/cam/realmonitor?channel=1&subtype=00"
```

H.264 1920x1080 10fps + 音声 PCM A-law が返る。SEI truncated 警告は無害。

## 今後作るもの（自前実装）

1. ~~イベント蓄積: Frigate API を定期取得して SQLite に永続化~~ → gatekeeper で実装済み
2. ~~統計ダッシュボード~~ → 実装済み（`http://<host>:8080/`）
3. ~~見知らぬ人アラート~~ → 危険度アラートとして実装済み（表情の加点は未実装）
4. カメラの首位置リセット対策（1〜2 日に一度、首位置が初期位置に戻る現象）
   - ONVIF（ポート 8080）は応答するが PTZ 設定を持たないプロファイルしか無く、Frigate からは首振りを制御できない（2026-09 調査）
   - 首振りはアプリ専用プロトコル（ポート 34567、Xiongmai 系）のみ。Python の `python-dvr` 等で
     プリセット移動を定期送信する案があるが未検証
   - まずカメラアプリの「定時再起動」「クルーズ / 巡回」「自動追尾」設定を確認する

## 運用上の注意

- 顔が小さいと認識精度が大きく落ちる。インターホン付近・顔の高さ・正面・寄った画角にする。
  現在の画角では顔が 40〜100px しかないため、Frigate の `face_recognition.min_area` は 1600（40x40）、
  gatekeeper の `MIN_FACE_PX` は 36 にしてある。これ以上厳しくすると本人の顔も弾かれる
- 通行人（張り紙をする不審者など）も分類対象にするため、ゾーンによる絞り込みはしていない。
  カメラの向きが変わっても設定変更は不要
- 同じ人に別 ID が付くことがある（登録画像が少ないうち、昼夜で見え方が違うとき）。`merge` で統合する
- 未登録顔などの個人データは保持期限を決めて自動削除し、外部公開しない
- Git に入れないもの: `.env`、`media/`、`config/` 配下の生成物（DB・鍵・顔画像）
