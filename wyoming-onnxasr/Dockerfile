FROM python:3.11-slim

RUN pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    "onnx-asr[cpu,hub]" wyoming

COPY server.py /app/server.py

WORKDIR /app

ENTRYPOINT ["python", "server.py"]
