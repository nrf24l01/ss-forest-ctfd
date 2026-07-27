# Territory Control for CTFd

CTFd plugin implementing team Attack Points, physical-device territory attacks, and periodic official-score awards.

## Run with Docker

1. Copy `.env.example` to `.env` and replace `TERRITORY_DEVICE_SECRET` with a long random value: `cp .env.example .env`.
2. Run `docker compose up --build`.
3. Open `http://localhost:8000`, complete CTFd setup, then create territories at `/admin/territory-control`.
4. Create challenges with type **Territory Attack Points**. Their `Attack Points` field credits team AP; their CTFd value is always zero.

The `serial-bridge` service requires a real `SERIAL_PORT`. For development without physical hardware, omit that service:

```sh
docker compose up --build ctfd territory-worker mariadb redis
```

## Device protocol

The root/device emits:

```text
UUID_REQUEST session=1 node=aa:bb:cc:dd:ee:ff uuid=<team-uuid>
```

`node` must match the Territory `Node ID` configured by an admin. The bridge posts the scan to CTFd and replies:

```text
color RRGGBB
```

Black (`000000`) means neutral, expired/no pending capture, an invalid scan, or an API failure.

## Attack flow

1. A signed-in player calls `POST /api/v1/territory-control/attacks` with `territory_id` and decimal `attack_points`.
2. The requested AP is reserved for 30 seconds.
3. The device must scan the matching team QR UUID during the reservation.
4. On a match, `remaining = old_defense * TERRITORY_DEFENSE_MULTIPLIER - spent_ap * TERRITORY_ATTACK_MULTIPLIER`.
5. Positive remaining defense stays with the current owner. Zero becomes neutral/black. A negative result becomes the attacking team's defense using its absolute value.
6. An expired reservation is refunded by the worker. A mismatched scan leaves the valid reservation pending until its matching scan or expiry.

Final Score payout amounts must be whole numbers because CTFd's native `Awards.value` column is integer-only. Combat values, Attack Points, and both multipliers remain decimal.

Team UUID and QR image are available to signed-in players at `/api/v1/territory-control/me` and `/api/v1/territory-control/me/qr`.
