# Territory Control Deployment and Operations Guide

## What this provides

Territory Control adds a second, team-owned currency to CTFd:

- **Attack Points (AP)** are earned from Territory Attack Points challenges and spent to attack physical territories.
- **Final Score** remains CTFd's native score. It is awarded periodically to territory owners and appears on the normal CTFd scoreboard.
- The player-facing Territory Control page is `/territory-control`.

## Requirements

- Docker Engine with Docker Compose v2.
- A working CTFd Docker deployment, or this repository as the CTFd build context.
- A MariaDB and Redis service reachable by CTFd.
- For physical scanning: a host with access to the root controller serial device.

## Deploy CTFd and the plugin

### New local installation

1. Create the environment file:

```sh
cp .env.example .env
```

2. Generate a device secret and put it in `.env`:

```sh
openssl rand -hex 32
```

3. Set the serial values only when a device is attached:

```dotenv
TERRITORY_DEVICE_SECRET=<generated-secret>
SERIAL_PORT=/dev/ttyUSB0
SERIAL_BAUD=115200
TERRITORY_NODE_ID=aa:bb:cc:dd:ee:ff
```

4. Start the complete deployment:

```sh
docker compose up -d --build
```

For development without physical hardware, omit the device driver:

```sh
docker compose up -d --build ctfd territory-worker mariadb redis
```

5. Open CTFd and complete its initial setup. Then open `/admin/territory-control` to create territories.

### Existing CTFd Docker installation

Use the overlay in `deploy/docker-compose.territory-control.yml`. The CTFd image must contain this repository's `territory_control/` directory at:

```text
CTFd/plugins/territory_control/
```

Create a private environment file outside Git:

```dotenv
TERRITORY_DEVICE_SECRET=<generated-secret>
TERRITORY_DEFENSE_MULTIPLIER=1
TERRITORY_ATTACK_MULTIPLIER=2
```

Build and start the plugin and worker:

```sh
docker compose \
  --env-file .env.territory-control \
  -f docker-compose.yml \
  -f deploy/docker-compose.territory-control.yml \
  up -d --build ctfd territory-worker
```

The `territory-worker` service must run:

```text
python -m CTFd.plugins.territory_control.worker
```

It must not run a second Gunicorn/CTFd web server.

If Nginx returns `502` immediately after CTFd is recreated, restart only Nginx so it resolves the new CTFd container IP:

```sh
docker compose restart nginx
```

## Configure the game

### Create territories

As an admin, open `/admin/territory-control` and add each physical territory:

- **Name**: player-visible territory name.
- **Node ID**: the root device identifier, for example `aa:bb:cc:dd:ee:ff`.
- **Defense Points**: initial defense AP.
- **Final Score award**: integer CTFd score paid to the owner each award interval.

Use unique Node IDs. A root controller can only scan for the territory configured with its Node ID.

### Create AP challenges

In `/admin/challenges`, choose **Territory Attack Points** as the challenge type.

- Set **Attack Points** to the AP reward.
- Add at least one normal CTFd flag.
- The challenge's CTFd value is always `0`; it does not add Final Score directly.

On the player challenge board, these challenges display their reward as `<count> AP`, not CTFd points.

Do not create AP challenges as the normal `standard` type. Standard challenge solves do not credit AP.

## Run the independent device driver

The driver does not import CTFd or share CTFd's filesystem. It needs network access to the CTFd API and access to the serial port.

```sh
cd territory_device_driver
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Set these values in the driver environment:

```dotenv
CTFD_URL=http://127.0.0.1:8000
TERRITORY_DEVICE_SECRET=<same-secret-as-CTFd>
TERRITORY_NODE_ID=aa:bb:cc:dd:ee:ff
SERIAL_PORT=/dev/ttyUSB0
SERIAL_BAUD=115200
```

Start it:

```sh
python serial_controller.py
```

When the driver runs on the CTFd host, use `http://127.0.0.1:8000` for `CTFD_URL`. If it uses the public reverse-proxy URL, the proxy must forward the `X-Territory-Secret` request header.

The root must emit scans in this format:

```text
UUID_REQUEST session=1 node=aa:bb:cc:dd:ee:ff uuid=<team-uuid>
```

The driver replies to serial with:

```text
color RRGGBB
```

`000000` means no valid capture session or no eligible scan.

## Player workflow

1. Players solve **Territory Attack Points** challenges to earn AP.
2. Their AP balance appears separately on their team profile and on `/territory-control`.
3. The QR code on `/territory-control` encodes `ss-forest://<team-uuid>`.
4. A player selects a territory, enters AP to spend, and starts the attack.
5. The device has 30 seconds to scan the attacking team's QR code.
6. The result color is written back to the physical root controller.

The normal CTFd `/scoreboard` is Final Score only. It is not the AP balance.

## Combat and payout behavior

Combat uses decimal values:

```text
remaining = old_defense * TERRITORY_DEFENSE_MULTIPLIER
            - spent_ap * TERRITORY_ATTACK_MULTIPLIER
```

- Positive `remaining`: current owner keeps the territory with that defense.
- Zero: territory becomes neutral.
- Negative: attacker captures the territory with `abs(remaining)` defense.
- No valid scan before expiry: reserved AP is refunded by `territory-worker`.

Final Score awards are integers because CTFd native award values are integer-only.

## Validate and troubleshoot

Check service state:

```sh
docker compose ps
docker compose logs --tail=100 ctfd territory-worker
```

Verify the device channel from the CTFd host without revealing the secret:

```sh
curl -o /dev/null -w '%{http_code}\n' \
  -H "X-Territory-Secret: $TERRITORY_DEVICE_SECRET" \
  'http://127.0.0.1:8000/api/v1/territory-control/device/commands?node_id=aa:bb:cc:dd:ee:ff'
```

Expected responses:

- `204`: device is authenticated and has no queued command.
- `200`: a command is queued for the device.
- `400`: Node ID is missing.
- `403`: wrong secret, missing secret, or a proxy removed the request header.

Run the combat tests before deployment changes:

```sh
python -m unittest discover -s tests -v
```
