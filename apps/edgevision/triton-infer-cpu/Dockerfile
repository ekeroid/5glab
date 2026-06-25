FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    opencv-python-headless \
    numpy \
    'grpcio>=1.60' \
    'grpcio-tools>=1.60' \
    onnxruntime \
    ultralytics

WORKDIR /opt/sidecar
COPY proto/ /opt/proto/
COPY main.py .
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh
RUN python3 -m grpc_tools.protoc -I/opt/proto --python_out=. --grpc_python_out=. /opt/proto/infer.proto

EXPOSE 8080 50051

ENTRYPOINT ["/opt/entrypoint.sh"]
