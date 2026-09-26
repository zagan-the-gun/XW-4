"""性別・年齢の推定（InsightFace genderage.onnx を OpenCV DNN で実行）。

入力は顔の位置が分かっている画像。顔枠の中心を基準に 1.5 倍の正方形を 96x96 に正規化する
（insightface の Attribute.get と同じ前処理）。小さい顔・暗所では精度が落ちるので「推定」として扱う。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class GenderResult:
    gender: str          # "male" / "female"
    confidence: float    # 0.5〜1.0（2 クラスの softmax）
    age: int


def is_grayscale(img, tolerance: float = 3.0) -> bool:
    """3 チャンネルの差がほぼ無い（赤外線モノクロ）画像か。"""
    import numpy as np

    if img.ndim < 3 or img.shape[2] < 3:
        return True
    b, g, r = img[:, :, 0].astype(np.int16), img[:, :, 1].astype(np.int16), img[:, :, 2].astype(np.int16)
    return float(np.mean(np.abs(b - g)) + np.mean(np.abs(g - r))) / 2 < tolerance


class GenderEstimator:
    def __init__(self, model_path: Optional[str]):
        self.net = None
        if not model_path:
            return
        try:
            import cv2

            self._cv2 = cv2
            self.net = cv2.dnn.readNetFromONNX(model_path)
        except Exception as e:  # noqa: BLE001
            log.warning("性別推定モデルを読み込めないため無効化します: %s", e)
            self.net = None

    @property
    def enabled(self) -> bool:
        return self.net is not None

    def estimate(self, image: bytes, box: Tuple[float, float, float, float]) -> Optional[GenderResult]:
        """box は (x, y, w, h)。facecheck が返す座標（余白を足した画像上）に合わせ、同じ余白を足して使う。"""
        if self.net is None:
            return None
        cv2 = self._cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if is_grayscale(img):
            return None  # 夜間の赤外線映像は色情報が無く、モデルの前提と違うので推定しない
        h, w = img.shape[:2]
        img = cv2.copyMakeBorder(img, h // 2, h // 2, w // 2, w // 2, cv2.BORDER_CONSTANT, value=(128, 128, 128))
        x, y, fw, fh = box
        cx, cy = x + fw / 2, y + fh / 2
        scale = 96.0 / (max(fw, fh) * 1.5)
        m = np.array([[scale, 0, 48 - cx * scale], [0, scale, 48 - cy * scale]], dtype=np.float32)
        aligned = cv2.warpAffine(img, m, (96, 96), borderValue=(0, 0, 0))
        blob = cv2.dnn.blobFromImage(aligned, 1.0, (96, 96), (0, 0, 0), swapRB=True)
        self.net.setInput(blob)
        out = self.net.forward()[0]
        f, mm = float(out[0]), float(out[1])
        e = np.exp(np.array([f, mm]) - max(f, mm))
        p = e / e.sum()
        gender = "male" if mm > f else "female"
        return GenderResult(gender, float(p.max()), int(round(float(out[2]) * 100)))
