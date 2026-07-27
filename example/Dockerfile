FROM python:3.12-slim

WORKDIR /app
COPY example/serial_bridge.py /app/serial_bridge.py
RUN pip install --no-cache-dir pyserial requests
CMD ["python", "/app/serial_bridge.py"]
