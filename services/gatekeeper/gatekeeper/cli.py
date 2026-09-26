"""コマンドライン: run / once / persons / visits / rename / merge"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from .config import Settings
from .db import Database
from .service import Gatekeeper


def _fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def build(cfg: Settings) -> Gatekeeper:
    from .facecheck import FaceCheck
    from .frigate import FrigateClient
    from .gender import GenderEstimator

    client = FrigateClient(cfg.frigate_url)
    db = Database(os.path.join(cfg.data_dir, "gatekeeper.db"))
    facecheck = FaceCheck(cfg.face_model, cfg.face_score) if cfg.face_model else None
    if facecheck is not None and not facecheck.enabled:
        facecheck = None
    gender = GenderEstimator(cfg.gender_model) if cfg.gender_model else None
    if gender is not None and not gender.enabled:
        gender = None
    from .notify import DiscordNotifier

    notifier = DiscordNotifier(cfg.discord_webhook_url)
    return Gatekeeper(client, db, cfg, facecheck=facecheck, gender=gender, notifier=notifier)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="gatekeeper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="常駐して定期処理し、ダッシュボードを配信する")
    sub.add_parser("once", help="1 回だけ処理して終了")
    sub.add_parser("backfill-gender", help="既存の訪問記録に性別・年齢推定を付け直す")
    cl = sub.add_parser("clean", help="Frigate の登録画像を品質チェックにかけ、通らないものを削除する")
    cl.add_argument("--dry-run", action="store_true", help="削除せず結果だけ表示")
    rl = sub.add_parser("relink", help="見た目による紐付けを消してやり直す（しきい値変更後など）")
    rl.add_argument("--hours", type=float, default=48.0)
    sub.add_parser("persons", help="人物一覧")
    v = sub.add_parser("visits", help="訪問記録")
    v.add_argument("--person", help="人物 ID で絞る")
    v.add_argument("--limit", type=int, default=30)
    r = sub.add_parser("rename", help="表示名を付ける（Frigate 側の ID は変わらない）")
    r.add_argument("person_id")
    r.add_argument("display_name", nargs="?", default=None, help="省略で表示名を消す")
    m = sub.add_parser("merge", help="同一人物に振られた 2 つの ID を統合する（src を dst へ）")
    m.add_argument("src")
    m.add_argument("dst")
    pg = sub.add_parser("purge", help="誤登録の ID を削除する（Frigate の画像も消し、訪問は未特定に戻す）")
    pg.add_argument("person_id")
    args = p.parse_args(argv)

    cfg = Settings.from_env()
    if args.cmd == "run":
        from .web import start_web

        gk = build(cfg)
        gk.install_signal_handlers()
        start_web(cfg, os.path.join(cfg.data_dir, "gatekeeper.db"), gatekeeper=gk, client=gk.client)
        gk.run_forever()
        return 0
    if args.cmd == "once":
        print(build(cfg).process_once())
        return 0
    if args.cmd == "backfill-gender":
        print(build(cfg).backfill_gender())
        return 0
    if args.cmd == "relink":
        n = build(cfg).relink_appearance(args.hours)
        print(f"{n} 件の来訪を未確認に戻しました。次の周期から順に紐付け直します")
        return 0
    if args.cmd == "clean":
        try:
            rep = build(cfg).clean_library(dry_run=args.dry_run)
        except ValueError as e:
            print(e)
            return 1
        for name, r in rep.items():
            print(f"{name}: {r['total']} 枚 → 残す {r['kept']} / 削除 {r['deleted']}" + ("（dry-run）" if args.dry_run else ""))
        return 0
    db = Database(os.path.join(cfg.data_dir, "gatekeeper.db"))
    if args.cmd == "persons":
        rows = db.persons()
        print(f"{'ID':8} {'表示名':12} {'訪問':>4}  {'最終訪問':19}  {'登録日':19}")
        for r in rows:
            print(f"{r['id']:8} {(r['display_name'] or '-'):12} {r['visit_count']:>4}  {_fmt(r['last_seen']):19}  {_fmt(r['created_at']):19}")
        return 0
    if args.cmd == "visits":
        names = {r["id"]: r["display_name"] for r in db.persons()}
        print(f"{'開始':19} {'滞在':>6} {'人物':8} {'表示名':12} {'判定':12} {'score':>5}  event")
        for r in db.visits(args.person, args.limit):
            pid = r["person_id"] or "-"
            dur = f"{r['duration']:.0f}s" if r["duration"] is not None else "-"
            sc = f"{r['score']:.2f}" if r["score"] is not None else "-"
            print(f"{_fmt(r['start_time']):19} {dur:>6} {pid:8} {(names.get(pid) or '-'):12} {r['method']:12} {sc:>5}  {r['event_id']}")
        return 0
    if args.cmd == "rename":
        ok = db.rename_person(args.person_id, args.display_name)
        print("更新しました" if ok else f"人物 {args.person_id} が見つかりません")
        return 0 if ok else 1
    if args.cmd == "merge":
        try:
            res = build(cfg).merge(args.src, args.dst)
        except ValueError as e:
            print(f"統合できません: {e}")
            return 1
        except Exception as e:  # noqa: BLE001  Frigate との通信エラーなど
            print(f"統合が途中で失敗しました（もう一度実行すると続きから完了します）: {e}")
            return 1
        print(f"{args.src} -> {args.dst}: 画像 {res['images_moved']} 枚、訪問 {res['visits_moved']} 件を移動しました")
        return 0
    if args.cmd == "purge":
        try:
            res = build(cfg).purge(args.person_id)
        except ValueError as e:
            print(f"削除できません: {e}")
            return 1
        print(f"{args.person_id}: 画像 {res['images_deleted']} 枚を削除、訪問 {res['visits_cleared']} 件を未特定に戻しました")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
