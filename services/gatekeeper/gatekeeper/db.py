"""SQLite: 人物（ID と表示名）、訪問記録、別名（統合履歴）、内部状態。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Set

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
  id           TEXT PRIMARY KEY,
  display_name TEXT,
  created_at   REAL NOT NULL,
  last_seen    REAL,
  visit_count  INTEGER NOT NULL DEFAULT 0,
  note         TEXT
);
CREATE TABLE IF NOT EXISTS visits (
  event_id   TEXT PRIMARY KEY,
  camera     TEXT NOT NULL,
  person_id  TEXT,
  start_time REAL NOT NULL,
  end_time   REAL,
  duration   REAL,
  method     TEXT NOT NULL,
  score      REAL,
  face_file  TEXT,
  zones      TEXT,
  reason     TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS visits_person ON visits(person_id, start_time);
CREATE INDEX IF NOT EXISTS visits_start ON visits(start_time);
CREATE TABLE IF NOT EXISTS aliases (
  src TEXT PRIMARY KEY,
  dst TEXT NOT NULL,
  merged_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  event_id    TEXT PRIMARY KEY,   -- 来訪（グループ）の先頭イベント
  subject     TEXT,               -- 人物 ID / 見た目グループ / NULL
  level       TEXT NOT NULL,
  score       INTEGER NOT NULL,
  reasons     TEXT NOT NULL,      -- JSON 配列
  start_time  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  notified_at REAL,
  notify_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_time ON alerts(start_time);
"""

PENDING = "pending"


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(visits)")}
        with self.conn:
            for col, typ in (("gender", "TEXT"), ("gender_score", "REAL"), ("age", "INTEGER"),
                             ("appearance_person", "TEXT"), ("appearance_group", "TEXT"),
                             ("appearance_score", "REAL"), ("appearance_ref", "TEXT"),
                             ("appearance_checked", "INTEGER NOT NULL DEFAULT 0")):
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE visits ADD COLUMN {col} {typ}")

    # --- アラート ---
    def get_alert(self, event_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM alerts WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def upsert_alert(self, event_id: str, subject: Optional[str], level: str, score: int, reasons: list,
                     start_time: float, now: float, notified: bool) -> None:
        prev = self.get_alert(event_id)
        with self.conn:
            self.conn.execute(
                """INSERT INTO alerts(event_id, subject, level, score, reasons, start_time, updated_at, notified_at, notify_count)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET subject=excluded.subject, level=excluded.level, score=excluded.score,
                     reasons=excluded.reasons, updated_at=excluded.updated_at,
                     notified_at=COALESCE(excluded.notified_at, alerts.notified_at), notify_count=excluded.notify_count""",
                (event_id, subject, level, score, json.dumps(reasons, ensure_ascii=False), start_time, now,
                 now if notified else None, (prev["notify_count"] if prev else 0) + (1 if notified else 0)),
            )

    def alerts(self, since: Optional[float] = None, limit: int = 50) -> List[dict]:
        if since is None:
            rows = self.conn.execute("SELECT * FROM alerts ORDER BY start_time DESC LIMIT ?", (limit,))
        else:
            rows = self.conn.execute("SELECT * FROM alerts WHERE start_time>=? ORDER BY start_time DESC LIMIT ?", (since, limit))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["reasons"] = json.loads(d["reasons"])
            except ValueError:
                d["reasons"] = []
            out.append(d)
        return out

    def last_notified(self, subject: Optional[str], since: float) -> Optional[float]:
        if subject is None:
            return None
        row = self.conn.execute(
            "SELECT MAX(notified_at) AS t FROM alerts WHERE subject=? AND notified_at>=?", (subject, since)
        ).fetchone()
        return row["t"] if row and row["t"] else None

    def notifications_since(self, since: float) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE notified_at>=?", (since,)).fetchone()
        return int(row["n"]) if row else 0

    # --- 見た目による紐付け ---
    def visit(self, event_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM visits WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def unchecked_visits(self, since: float, before: float, limit: int) -> List[dict]:
        """見た目の確認がまだで、終了から少し経った未特定の来訪（古い順）。"""
        rows = self.conn.execute(
            """SELECT * FROM visits WHERE appearance_checked=0 AND method<>? AND person_id IS NULL
               AND start_time>=? AND COALESCE(end_time,start_time)<=? ORDER BY start_time LIMIT ?""",
            (PENDING, since, before, limit),
        )
        return [dict(r) for r in rows]

    def mark_appearance_checked(self, event_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE visits SET appearance_checked=1 WHERE event_id=?", (event_id,))

    def set_appearance(self, event_id: str, person: Optional[str], group: Optional[str], score: float, ref: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE visits SET appearance_person=?, appearance_group=?, appearance_score=?, appearance_ref=?, appearance_checked=1 WHERE event_id=?",
                (person, group, score, ref, event_id),
            )

    def set_appearance_group(self, event_id: str, group: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE visits SET appearance_group=? WHERE event_id=? AND appearance_group IS NULL", (group, event_id))

    def promote_group(self, group: str, person: str) -> int:
        """見た目グループの全メンバーを「見た目で person と推定」に格上げする。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE visits SET appearance_person=? WHERE appearance_group=? AND person_id IS NULL AND appearance_person IS NULL",
                (person, group),
            )
        return cur.rowcount

    def group_anchor(self, group: str) -> Optional[str]:
        """見た目グループの起点（最初の来訪）の event_id。"""
        row = self.conn.execute(
            "SELECT event_id FROM visits WHERE appearance_group=? ORDER BY start_time LIMIT 1", (group,)
        ).fetchone()
        return row["event_id"] if row else None

    def reset_appearance(self, since: float) -> int:
        """見た目の紐付けをやり直すために、期間内の結果を消して未確認に戻す。"""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE visits SET appearance_person=NULL, appearance_group=NULL, appearance_score=NULL,
                   appearance_ref=NULL, appearance_checked=0 WHERE start_time>=?""",
                (since,),
            )
        return cur.rowcount

    def next_group_id(self) -> str:
        seq = int(self.get_state("max_group_seq", "0") or 0) + 1
        self.set_state("max_group_seq", str(seq))
        return f"a{seq:04d}"

    def clear_appearance_for_person(self, pid: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE visits SET appearance_person=NULL WHERE appearance_person=?", (pid,))

    def rename_appearance_person(self, src: str, dst: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE visits SET appearance_person=? WHERE appearance_person=?", (dst, src))

    def set_visit_gender(self, event_id: str, gender: Optional[str], score: Optional[float], age: Optional[int]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE visits SET gender=?, gender_score=?, age=? WHERE event_id=?", (gender, score, age, event_id)
            )

    def visits_with_faces(self, limit: int = 100000) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM visits WHERE face_file IS NOT NULL AND method<>? ORDER BY start_time DESC LIMIT ?", (PENDING, limit)
        )
        return [dict(r) for r in rows]

    def all_visits(self, since: Optional[float] = None) -> List[dict]:
        if since is None:
            rows = self.conn.execute("SELECT * FROM visits ORDER BY start_time")
        else:
            rows = self.conn.execute("SELECT * FROM visits WHERE start_time>=? ORDER BY start_time", (since,))
        return [dict(r) for r in rows]

    # --- state ---
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_json(self, key: str, default):
        raw = self.get_state(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value) -> None:
        self.set_state(key, json.dumps(value))

    # --- persons ---
    def persons(self) -> List[dict]:
        rows = self.conn.execute("SELECT * FROM persons ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def person_ids(self) -> Set[str]:
        return {r["id"] for r in self.conn.execute("SELECT id FROM persons")}

    def ensure_person(self, pid: str, created_at: Optional[float] = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO persons(id, created_at) VALUES(?, ?)", (pid, created_at or time.time())
            )

    def refresh_person(self, pid: str, seen_at: Optional[float] = None) -> None:
        """訪問回数と最終訪問を visits から計算し直す（再実行しても二重に数えず、訪問を取り消せば戻る）。"""
        self.ensure_person(pid)
        with self.conn:
            self.conn.execute(
                """UPDATE persons SET
                     visit_count=(SELECT COUNT(*) FROM visits WHERE person_id=? AND method<>?),
                     last_seen=NULLIF(MAX(COALESCE(?,0),
                                   COALESCE((SELECT MAX(COALESCE(end_time,start_time)) FROM visits WHERE person_id=? AND method<>?),0)), 0)
                   WHERE id=?""",
                (pid, PENDING, seen_at, pid, PENDING, pid),
            )

    def rename_person(self, pid: str, display_name: Optional[str]) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE persons SET display_name=? WHERE id=?", (display_name, pid))
        return cur.rowcount > 0

    def delete_person(self, pid: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM persons WHERE id=?", (pid,))

    def expired_persons(self, prefix: str, older_than: float, max_visits: int) -> List[str]:
        """自動発行 ID で、訪問が少なく長く見ていない人物。"""
        rows = self.conn.execute(
            "SELECT id, last_seen, created_at, visit_count FROM persons WHERE id LIKE ? AND display_name IS NULL",
            (prefix + "%",),
        ).fetchall()
        pat = re.compile(rf"^{re.escape(prefix)}\d+$")
        out = []
        for r in rows:
            if not pat.match(r["id"]):
                continue
            seen = r["last_seen"] or r["created_at"]
            if r["visit_count"] <= max_visits and seen < older_than:
                out.append(r["id"])
        return out

    # --- ID 発行: peek で候補を見て、登録が成功したときだけ commit で確定する ---
    def _max_seq(self, prefix: str, extra_names: Iterable[str]) -> int:
        pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        used = int(self.get_state("max_person_seq", "0") or 0)
        names = list(self.person_ids()) + list(extra_names)
        names += [r["src"] for r in self.conn.execute("SELECT src FROM aliases")]
        names += [r["person_id"] for r in self.conn.execute("SELECT DISTINCT person_id FROM visits WHERE person_id IS NOT NULL")]
        for name in names:
            m = pat.match(name or "")
            if m:
                used = max(used, int(m.group(1)))
        return used

    def peek_person_id(self, prefix: str, digits: int, extra_names: Iterable[str] = ()) -> str:
        return f"{prefix}{self._max_seq(prefix, extra_names) + 1:0{digits}d}"

    def commit_person_seq(self, pid: str, prefix: str) -> None:
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", pid)
        if not m:
            return
        cur = int(self.get_state("max_person_seq", "0") or 0)
        self.set_state("max_person_seq", str(max(cur, int(m.group(1)))))

    # --- aliases（統合履歴） ---
    def resolve_alias(self, name: Optional[str]) -> Optional[str]:
        seen = set()
        while name and name not in seen:
            seen.add(name)
            row = self.conn.execute("SELECT dst FROM aliases WHERE src=?", (name,)).fetchone()
            if not row:
                break
            name = row["dst"]
        return name

    def merge_person(self, src: str, dst: str, prefix: str = "") -> int:
        """src の訪問を dst に付け替え、src を削除し、別名として記録する。付け替えた件数を返す。"""
        self.ensure_person(dst)
        with self.conn:
            cur = self.conn.execute("UPDATE visits SET person_id=? WHERE person_id=?", (dst, src))
            self.conn.execute("DELETE FROM persons WHERE id=?", (src,))
            self.conn.execute(
                "INSERT OR REPLACE INTO aliases(src, dst, merged_at) VALUES(?,?,?)", (src, dst, time.time())
            )
            self.conn.execute("UPDATE aliases SET dst=? WHERE dst=?", (dst, src))
        if prefix:
            self.commit_person_seq(src, prefix)
            self.commit_person_seq(dst, prefix)
        self.refresh_person(dst)
        return cur.rowcount

    def merged_event_ids(self, dst: str) -> List[str]:
        return [r["event_id"] for r in self.conn.execute("SELECT event_id FROM visits WHERE person_id=?", (dst,))]

    # --- visits ---
    def visit_method(self, event_id: str) -> Optional[str]:
        row = self.conn.execute("SELECT method FROM visits WHERE event_id=?", (event_id,)).fetchone()
        return row["method"] if row else None

    def has_visit(self, event_id: str) -> bool:
        """終わった記録があるか（処理中の行は含めない）。"""
        m = self.visit_method(event_id)
        return m is not None and m != PENDING

    def visit_pending(self, event_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT person_id, face_file FROM visits WHERE event_id=? AND method=?", (event_id, PENDING)
        ).fetchone()
        return dict(row) if row else None

    def record_visit(
        self,
        event_id: str,
        camera: str,
        person_id: Optional[str],
        start_time: float,
        end_time: Optional[float],
        method: str,
        score: Optional[float] = None,
        face_file: Optional[str] = None,
        zones: Optional[list] = None,
        reason: str = "",
    ) -> None:
        duration = (end_time - start_time) if end_time else None
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO visits
                   (event_id, camera, person_id, start_time, end_time, duration, method, score, face_file, zones, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, camera, person_id, start_time, end_time, duration, method, score,
                    face_file, json.dumps(zones or []), reason, time.time(),
                ),
            )

    def visits(self, person_id: Optional[str] = None, limit: int = 50) -> List[dict]:
        if person_id:
            rows = self.conn.execute(
                "SELECT * FROM visits WHERE person_id=? ORDER BY start_time DESC LIMIT ?", (person_id, limit)
            )
        else:
            rows = self.conn.execute("SELECT * FROM visits ORDER BY start_time DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def face_files(self) -> Dict[str, str]:
        """{face_file: event_id}"""
        return {
            r["face_file"]: r["event_id"]
            for r in self.conn.execute("SELECT event_id, face_file FROM visits WHERE face_file IS NOT NULL")
        }

    def null_face_files(self, files: Iterable[str]) -> None:
        files = list(files)
        if not files:
            return
        with self.conn:
            self.conn.executemany("UPDATE visits SET face_file=NULL WHERE face_file=?", [(f,) for f in files])
