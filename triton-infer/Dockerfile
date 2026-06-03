FROM nvcr.io/nvidia/tritonserver:24.10-py3

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    'setuptools<81' \
    fastapi \
    uvicorn \
    'tritonclient[grpc]' \
    opencv-python-headless \
    'grpcio>=1.81' \
    'grpcio-tools>=1.81'

COPY models/ /models/
COPY proto/ /opt/proto/
COPY sidecar/ /opt/sidecar/
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh
RUN python3 -m grpc_tools.protoc -I/opt/proto --python_out=/opt/sidecar --grpc_python_out=/opt/sidecar /opt/proto/infer.proto

EXPOSE 8000 8001 8080 50051

ENTRYPOINT ["/opt/entrypoint.sh"]
