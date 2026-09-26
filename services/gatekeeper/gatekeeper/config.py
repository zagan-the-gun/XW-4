from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass
class Settings:
    frigate_url: str = "http://frigate:5000"
    camera: str = "entrance"
    data_dir: str = "/data"
    poll_interval: float = 15.0
    # 初回起動時に何時間前のイベントまで遡って処理するか
    lookback_hours: float = 24.0
    # 終了済みイベントを取り直す重なり幅（秒）。周期の合間に始まって終わった短いイベントを取りこぼさないため
    overlap_seconds: float = 120.0
    # 試行画像の一致スコアがこれ以上なら「その人物」とみなす（Frigate の unknown_score に合わせる）
    merge_score: float = 0.85
    # 既存人物への画像追加（学習の補強）に使う試行画像の最低スコア
    reinforce_min_score: float = 0.95
    # 新しい ID を作るのに必要な試行画像の枚数
    min_attempts_new: int = 2
    # 顔画像の短辺がこれ未満なら学習には使わない（px）。この画角では顔が 40〜100px
    min_face_px: int = 36
    # 新しい ID を作るとき Frigate に登録する試行画像の枚数
    new_id_images: int = 5
    # 既存人物へ 1 イベントあたり追加する画像の上限。
    # 0 = 補強しない（既定）。誤認識した顔を取り込んで別人まで一致するようになる事故が起きたため
    reinforce_per_event: int = 0
    # 人物あたりの登録画像の上限
    max_images_per_person: int = 20
    # ID の形式: p0001
    id_prefix: str = "p"
    id_digits: int = 4
    # イベントに紐づかない古い試行画像を消すまでの時間
    stale_attempt_hours: float = 2.0
    # スナップショット経路で照合するときの人物枠の最小高さ（検知解像度の px）。小さいと顔も小さく信頼できない
    min_person_px: int = 250
    # 保留・失敗したイベントを諦めるまでの回数（poll_interval ごとに 1 回）
    retry_limit: int = 20
    # 「分類器を構築中」のとき同じ周期内でやり直すまでの待ち秒数
    recognize_retry_delay: float = 2.0
    # data/faces の顔画像コピーの保持日数（個人データなので期限を切って自動削除）
    retain_days: float = 90.0
    # 自動発行した ID のうち、訪問回数がこの回数以下で retain_days 以上見ていない人物は Frigate からも削除する
    expire_max_visits: int = 1
    # 自動発行する人物 ID の上限（超えたら新規 ID を作らず未特定として記録）
    max_persons: int = 500
    # 登録前の顔品質チェックに使う YuNet モデル（空ならチェックなし）
    face_model: str = ""
    # 品質チェックの顔検出しきい値（Frigate の API は 0.5 固定なので厳しめに）
    face_score: float = 0.8
    # 性別・年齢推定モデル（空なら推定しない）
    gender_model: str = ""
    # 統計で「同一人物の連続イベント」を 1 回の来訪にまとめる間隔（秒）
    visit_gap_seconds: float = 120.0
    # ダッシュボードの待ち受けポート（0 なら起動しない）
    web_port: int = 8080
    # 見た目（服装・持ち物）による紐付け。Frigate のセマンティック検索（サムネイル埋め込み）を使う
    appearance_enabled: bool = True
    # 前後この時間内の来訪とだけ比べる（同じ服装でいる時間の目安）
    appearance_window_hours: float = 24.0
    # 埋め込みの距離がこれ以下なら「同じ見た目」とみなす（小さいほど厳しい）。
    # 実測: 同じ人の連続イベントは 0.07〜0.11、別人は概ね 0.15 以上。ただし自転車同士・夜間同士は 0.12〜0.15 まで近づく
    appearance_max_distance: float = 0.11
    # 未特定同士をグループにまとめるときの、より厳しいしきい値。雨の日の傘・自転車など「場面の型」で別人が
    # 0.10〜0.12 まで近づくため、グループ化は同じ服装・持ち物でほぼ同じ見え方のものに限る
    appearance_group_max_distance: float = 0.09
    # 1 周期に見た目を確認する来訪の最大数（Frigate への検索回数）
    appearance_batch: int = 10
    # --- 危険度アラート ---
    # Discord Webhook URL（空なら通知しない。判定と画面表示はする）
    discord_webhook_url: str = ""
    # 通知に載せるダッシュボードの URL（例 http://192.168.11.13:8080/）
    dashboard_url: str = ""
    # 通知する最低レベル: low / medium / high
    alert_min_level: str = "medium"
    # 同じ人物・グループ・来訪への再通知を抑える時間（秒）
    alert_cooldown_seconds: float = 600.0
    # 1 時間あたりの通知上限
    alert_max_per_hour: int = 20
    # 評価対象にする来訪の範囲（分）
    alert_window_minutes: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs = {}
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is None or raw == "":
                continue
            try:
                kwargs[f.name] = _convert(f.default, raw)
            except ValueError:
                raise SystemExit(
                    f"環境変数 {f.name.upper()} の値が不正です: {raw!r}（{type(f.default).__name__} を期待）"
                ) from None
        cfg = cls(**kwargs)
        if cfg.poll_interval <= 0:
            raise SystemExit("POLL_INTERVAL は正の秒数にしてください")
        return cfg


def _convert(default, raw: str):
    raw = raw.strip()
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw
