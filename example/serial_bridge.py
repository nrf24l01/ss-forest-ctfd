#!/usr/bin/env python3
"""Bridge root UUID scans to Territory Control and return the resulting device color."""
import os
import re
import time

import requests
import serial


UUID_RE = re.compile(r"UUID_REQUEST\s+session=(?P<session>\d+)\s+node=(?P<node>[0-9a-fA-F:]{17})\s+uuid=(?P<uuid>\S*)")


def scan(api_url, secret, node_id, team_uuid):
    try:
        response = requests.post(
            f"{api_url.rstrip('/')}/api/v1/territory-control/device/scans",
            json={"node_id": node_id, "uuid": team_uuid},
            headers={"X-Territory-Secret": secret},
            timeout=5,
        )
        response.raise_for_status()
        return response.json().get("color", "000000")
    except (requests.RequestException, ValueError) as error:
        print(f"scan API failed: {error}")
        return "000000"


def main():
    api_url = os.environ["TERRITORY_API_URL"]
    secret = os.environ["TERRITORY_DEVICE_SECRET"]
    port = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("SERIAL_BAUD", "115200"))
    while True:
        try:
            with serial.Serial(port, baud, timeout=0.5) as device:
                print(f"listening on {port} at {baud}")
                while True:
                    raw_line = device.readline()
                    if not raw_line:
                        continue
                    match = UUID_RE.search(raw_line.decode("utf-8", errors="replace"))
                    if not match:
                        continue
                    color = scan(api_url, secret, match.group("node"), match.group("uuid"))
                    device.write(f"color {color}\n".encode("ascii"))
                    device.flush()
        except serial.SerialException as error:
            print(f"serial unavailable: {error}; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
