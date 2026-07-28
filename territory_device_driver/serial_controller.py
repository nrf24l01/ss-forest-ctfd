#!/usr/bin/env python3
"""Standalone serial driver for Territory Control physical nodes.

The driver does not access CTFd's database or filesystem. It long-polls CTFd
for scan commands, drives the serial root controller, and reports UUID scans.
"""
import argparse
import queue
import re
import threading
import time

import requests
import serial


UUID_RE = re.compile(r"UUID_REQUEST\s+session=(?P<session>\d+)\s+node=(?P<node>[0-9a-fA-F:]{17})\s+uuid=(?P<uuid>\S*)")


class TerritoryDriver:
    def __init__(self, args):
        self.args = args
        self.http = requests.Session()
        self.serial_lock = threading.Lock()
        self.stop = threading.Event()
        self.scans = queue.Queue()

    @property
    def headers(self):
        return {"X-Territory-Secret": self.args.secret}

    def api_url(self, path):
        return f"{self.args.server.rstrip('/')}{path}"

    def send_serial(self, device, command):
        with self.serial_lock:
            device.write((command + "\n").encode("ascii"))
            device.flush()

    def poll_commands(self, device):
        while not self.stop.is_set():
            try:
                response = self.http.get(
                    self.api_url("/api/v1/territory-control/device/commands"),
                    params={"node_id": self.args.node_id}, headers=self.headers, timeout=10,
                )
                if response.status_code == 204:
                    time.sleep(self.args.poll_interval)
                    continue
                response.raise_for_status()
                command = response.json()
                if command.get("type") == "start_scan":
                    self.send_serial(device, self.args.scan_command.format(seconds=self.args.scan_seconds))
                    print(f"started scan for capture session {command['session_id']}")
            except (requests.RequestException, ValueError) as error:
                print(f"command poll failed: {error}")
                time.sleep(self.args.poll_interval)

    def report_scans(self, device):
        while not self.stop.is_set():
            node_id, team_uuid = self.scans.get()
            try:
                response = self.http.post(
                    self.api_url("/api/v1/territory-control/device/scans"),
                    json={"node_id": node_id, "uuid": team_uuid}, headers=self.headers, timeout=10,
                )
                payload = response.json() if response.content else {}
                color = payload.get("color", "000000")
            except (requests.RequestException, ValueError) as error:
                print(f"scan report failed: {error}")
                color = "000000"
            self.send_serial(device, f"color {color}")
            print(f"UUID scan node={node_id} -> #{color}")

    def run(self):
        with serial.Serial(self.args.port, self.args.baud, timeout=0.5) as device:
            commands = threading.Thread(target=self.poll_commands, args=(device,), daemon=True)
            reporter = threading.Thread(target=self.report_scans, args=(device,), daemon=True)
            commands.start()
            reporter.start()
            print(f"connected to {self.args.port}; serving node {self.args.node_id}")
            while True:
                raw = device.readline()
                if not raw:
                    continue
                match = UUID_RE.search(raw.decode("utf-8", errors="replace"))
                if match:
                    self.scans.put((match.group("node"), match.group("uuid")))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="CTFd base URL, e.g. https://ctfd.example")
    parser.add_argument("--secret", required=True, help="TERRITORY_DEVICE_SECRET")
    parser.add_argument("--node-id", required=True, help="Node ID configured for this territory")
    parser.add_argument("--port", required=True, help="Serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--scan-command", default="scan {seconds}", help="Root command used to begin QR scanning")
    parser.add_argument("--scan-seconds", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser.parse_args()


if __name__ == "__main__":
    TerritoryDriver(parse_args()).run()
