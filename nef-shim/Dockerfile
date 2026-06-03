FROM python:3.12-slim

RUN useradd -m -r nef
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. infer.proto

USER nef

EXPOSE 8000 50051
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
