"""Frigate 0.18 の HTTP API（認証なしポート 5000）クライアント。"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

try:  # テストでは requests なしでも import できるようにする
    import requests
except ImportError:  # pragma: no cover
    requests = None

log = logging.getLogger(__name__)
TRAIN_DIR = "train"


class FrigateError(RuntimeError):
    pass


def _plain(text: str, limit: int = 120) -> str:
    """エラー本文（nginx の HTML ページなど）をタグ無しの短い文字列にする。記録や画面に出すため。"""
    import re

    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


class FrigateClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        if requests is None:
            raise RuntimeError("requests がインストールされていません")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # Frigate の nginx は /api/ の GET を 5 秒キャッシュする。直前の変更を即時に見たいので常に回避する
        self.session.headers["X-Cache-Bypass"] = "1"

    # --- 基本 ---
    def _req(self, method: str, path: str, ok=(200,), **kw):
        r = self.session.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if r.status_code not in ok:
            raise FrigateError(f"{method} {path} -> {r.status_code}: {_plain(r.text)}")
        return r

    def version(self) -> str:
        return self._req("GET", "/api/version").text.strip()

    def detect_size(self, camera: str) -> Optional[tuple]:
        """カメラの検知解像度 (幅, 高さ)。取れなければ None。"""
        try:
            cfg = self._req("GET", "/api/config").json()
            d = cfg["cameras"][camera]["detect"]
            return (int(d["width"]), int(d["height"]))
        except (FrigateError, KeyError, ValueError, TypeError):
            return None

    # --- イベント ---
    def list_events(
        self,
        camera: str,
        after: Optional[float] = None,
        before: Optional[float] = None,
        in_progress: int = 0,
        limit: int = 100,
        label: str = "person",
        sort: Optional[str] = None,
    ) -> List[dict]:
        params = {
            "cameras": camera,
            "labels": label,
            "limit": limit,
            "include_thumbnails": 0,
            "in_progress": in_progress,
        }
        if sort:
            params["sort"] = sort  # date_asc / date_desc など
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return self._req("GET", "/api/events", params=params).json()

    def get_event(self, event_id: str) -> Optional[dict]:
        r = self._req("GET", f"/api/events/{event_id}", ok=(200, 404))
        return r.json() if r.status_code == 200 else None

    def event_snapshot(self, event_id: str, crop: bool = True, quality: int = 90) -> Optional[bytes]:
        r = self._req(
            "GET",
            f"/api/events/{event_id}/snapshot.jpg",
            ok=(200, 404),
            # bbox/timestamp=0: 枠線・ラベル・時刻の描き込みなしの素の切り抜きを顔照合に渡す
            params={"crop": 1 if crop else 0, "bbox": 0, "timestamp": 0, "quality": quality},
        )
        return r.content if r.status_code == 200 else None

    def similar_events(self, event_id: str, after: float, before: float, limit: int = 20) -> Optional[List[dict]]:
        """サムネイルの見た目が似たイベント（Frigate セマンティック検索）。距離は search_distance（小さいほど似ている）。
        セマンティック検索が無効なら None。"""
        r = self._req(
            "GET", "/api/events/search", ok=(200, 400, 404),
            params={"search_type": "similarity", "event_id": event_id, "after": after, "before": before,
                    "limit": limit, "include_thumbnails": 0},
        )
        if r.status_code == 404:
            return []
        if r.status_code == 400:
            return None
        data = r.json()
        return data if isinstance(data, list) else None

    def event_clip_stream(self, event_id: str, chunk_size: int = 256 * 1024):
        """録画クリップ（mp4）をチャンクで返すイテレータ。無ければ None。"""
        r = self.session.get(f"{self.base}/api/events/{event_id}/clip.mp4", stream=True, timeout=(10, 120))
        if r.status_code != 200:
            r.close()
            return None
        return r.iter_content(chunk_size=chunk_size)

    def set_sub_label(self, event_id: str, label: str, score: Optional[float] = None) -> bool:
        body: Dict[str, object] = {"subLabel": label}
        if score is not None and 0 < score <= 1:
            body["subLabelScore"] = float(score)
        r = self._req("POST", f"/api/events/{event_id}/sub_label", ok=(200, 404), json=body)
        return r.status_code == 200

    # --- 顔ライブラリ ---
    def faces(self) -> Dict[str, List[str]]:
        """{名前: [画像ファイル名]}。未分類の試行画像は "train" キー。"""
        return self._req("GET", "/api/faces").json()

    def attempt_image(self, file: str) -> Optional[bytes]:
        return self.face_image(TRAIN_DIR, file)

    def face_image(self, name: str, file: str) -> Optional[bytes]:
        """顔ライブラリの画像（人物フォルダまたは train）。"""
        r = self._req("GET", f"/clips/faces/{name}/{file}", ok=(200, 404))
        return r.content if r.status_code == 200 else None

    def classify_attempt(self, name: str, file: str) -> bool:
        """train の試行画像を name に振り分ける（Frigate 側でファイル移動＋分類器再構築）。"""
        r = self._req(
            "POST", f"/api/faces/train/{name}/classify", ok=(200, 400, 404), json={"training_file": file}
        )
        ok = r.status_code == 200 and bool(r.json().get("success", False))
        if not ok:
            log.warning("classify %s -> %s 失敗: %s", file, name, r.text[:200])
        return ok

    def register_face(self, name: str, image: bytes, filename: str = "face.jpg") -> dict:
        """画像をアップロードして name に登録。Frigate が顔を検出して切り抜く。"""
        r = self._req(
            "POST", f"/api/faces/{name}/register", ok=(200, 400), files={"file": (filename, image, "image/jpeg")}
        )
        return r.json()

    def recognize(self, image: bytes, filename: str = "face.jpg") -> dict:
        """画像をアップロードして照合。{"success", "face_name", "score"}。顔なしは success False。"""
        r = self._req("POST", "/api/faces/recognize", ok=(200, 400), files={"file": (filename, image, "image/jpeg")})
        return r.json()

    def delete_faces(self, name: str, ids: Iterable[str]) -> None:
        ids = list(ids)
        if ids:
            self._req("POST", f"/api/faces/{name}/delete", json={"ids": ids})

    def reclassify(self, name: str, file: str, new_name: str) -> bool:
        r = self._req(
            "POST", f"/api/faces/{name}/reclassify", ok=(200, 400, 404), json={"id": file, "new_name": new_name}
        )
        return r.status_code == 200
