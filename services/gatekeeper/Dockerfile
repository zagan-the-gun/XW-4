FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Frigate と同じ顔検出モデル（YuNet）。登録前の品質チェックに使う
ADD https://github.com/NickM-27/facenet-onnx/releases/download/v1.0/facedet.onnx /models/facedet.onnx
# 性別・年齢推定（InsightFace buffalo_l の genderage、非商用研究ライセンス。自宅用途）
ADD https://huggingface.co/public-data/insightface/resolve/main/models/buffalo_l/genderage.onnx /models/genderage.onnx
RUN chmod 644 /models/facedet.onnx /models/genderage.onnx
COPY gatekeeper ./gatekeeper
COPY tests ./tests
ENV PYTHONUNBUFFERED=1 FACE_MODEL=/models/facedet.onnx GENDER_MODEL=/models/genderage.onnx
EXPOSE 8080
CMD ["python", "-m", "gatekeeper", "run"]
