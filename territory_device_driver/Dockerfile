FROM python:3.12-slim

WORKDIR /app
COPY territory_device_driver/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY territory_device_driver/serial_controller.py /app/serial_controller.py
ENTRYPOINT ["python", "/app/serial_controller.py"]
