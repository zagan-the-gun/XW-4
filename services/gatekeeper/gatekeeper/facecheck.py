"""登録前の顔品質チェック（Frigate と同じ YuNet モデルを使う）。

Frigate の登録/照合 API は検出しきい値 0.5 固定で、後頭部や頬の一部でも「顔あり」と判定して
登録してしまう。ここでは厳しめのしきい値と、両目・鼻の位置関係による正面判定、最小サイズを課す。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class FaceInfo:
    width: int
    height: int
    score: float
    frontal: bool
    # 余白を足した画像上の顔枠 (x, y, w, h)。性別推定に渡す
    box: tuple = (0, 0, 0, 0)

    @property
    def min_px(self) -> int:
        return min(self.width, self.height)


class FaceCheck:
    """YuNet で顔を検出し、最も大きい顔の情報を返す。モデルが無ければ None を返す（チェックなし扱い）。"""

    def __init__(self, model_path: Optional[str], score_threshold: float = 0.8):
        self.detector = None
        self.score_threshold = score_threshold
        if not model_path:
            return
        try:
            import cv2

            self._cv2 = cv2
            self.detector = cv2.FaceDetectorYN.create(
                model_path, "", (320, 320), score_threshold=score_threshold, nms_threshold=0.3, top_k=50
            )
        except Exception as e:  # noqa: BLE001
            log.warning("顔検出モデルを読み込めないため品質チェックを無効化します: %s", e)
            self.detector = None

    @property
    def enabled(self) -> bool:
        return self.detector is not None

    def check(self, image: bytes) -> Optional[FaceInfo]:
        if self.detector is None:
            return None
        cv2 = self._cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        # 顔ぴったりの切り抜きは検出器が苦手なので、周囲に 50% の余白を足す
        img = cv2.copyMakeBorder(img, h // 2, h // 2, w // 2, w // 2, cv2.BORDER_CONSTANT, value=(128, 128, 128))
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(img)
        if faces is None or len(faces) == 0:
            return None
        # 最も大きい顔
        f = max(faces, key=lambda r: float(r[2]) * float(r[3]))
        fw, fh = float(f[2]), float(f[3])
        # ランドマーク: 右目, 左目, 鼻, 右口角, 左口角（画像座標）。
        # 後頭部などの誤検出は目の間隔がほぼ 0 になり、鼻が目の外側に大きく外れる
        rx, ry, lx, ly, nx, ny = (float(v) for v in f[4:10])
        eye_dist = abs(lx - rx)
        nose_off = (nx - min(rx, lx)) / eye_dist if eye_dist > 1e-6 else 99.0
        frontal = eye_dist >= 0.15 * fw and -0.6 <= nose_off <= 1.7
        return FaceInfo(int(fw), int(fh), float(f[14]), frontal, (float(f[0]), float(f[1]), fw, fh))

    def acceptable(self, image: bytes, min_px: int) -> "tuple[bool, str]":
        """(合格か, 理由)。モデルが無いときは常に合格。"""
        ok, why, _ = self.inspect(image, min_px)
        return ok, why

    def inspect(self, image: bytes, min_px: int) -> "tuple[bool, str, Optional[FaceInfo]]":
        """(合格か, 理由, 顔情報)。"""
        if self.detector is None:
            return True, "no check", None
        info = self.check(image)
        if info is None:
            return False, "no face", None
        if info.min_px < min_px:
            return False, f"face too small ({info.width}x{info.height})", info
        if not info.frontal:
            return False, "face landmarks implausible", info
        return True, f"face {info.width}x{info.height} score {info.score:.2f}", info
