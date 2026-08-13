# Territory Control Deployment

## Components

- CTFd hosts the Territory Control plugin and the authoritative database state.
- `territory-worker` awards native CTFd points to captured territory owners.
- `serial-worker` is a standalone Go binary that runs on the host connected to the root serial device. It can be a different host from CTFd.

## CTFd setup

1. Copy `.env.example` to `.env` and set a long random `TERRITORY_DEVICE_SECRET`.
2. Start CTFd and the award worker:

```sh
docker compose up -d --build ctfd territory-worker mariadb redis
```

3. Complete CTFd setup, then use `/admin/territory-control` to create territories.
4. Configure each territory's root node MAC, defense, CTFd score value, and interval. For `+10 CTFd points every minute`, set score amount to `10` and interval seconds to `60`.
5. Create `Territory Attack Points` challenges to credit the separate team AP balance.

Admins can edit territory settings, force ownership/neutral status, inspect physical attack history, and adjust team AP from `/admin/territory-control`.

## Remote serial worker

Build the independent binary on the serial-device host:

```sh
cd serial_worker
go build -o serial-worker .
```

Run it with network access to CTFd and access to the root serial device:

```sh
./serial-worker \
  --server https://ctfd.example \
  --secret '<same-value-as-TERRITORY_DEVICE_SECRET>' \
  --port /dev/ttyUSB0 \
  --baud 115200
```

Use HTTPS or a private network. A reverse proxy must forward `X-Territory-Secret` unchanged. The worker neither imports CTFd nor accesses its filesystem or database.

## Configure territory

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

## Deploy personal challenge instances

`territory_owl` adds personal, team-scoped challenge containers. Its challenges
award normal CTFd team score and validate a per-instance `silCTF{...}` flag.

Copy the plugin into an existing CTFd installation at:

```text
CTFd/plugins/territory_owl/
```

For the repository deployment, create the ignored runtime directories and FRP
configuration files:

```sh
mkdir -p territory_owl_runtime/templates territory_owl_runtime/run deploy/territory_owl
```

`deploy/territory_owl/frps.ini` must contain the configured token:

```ini
[common]
bind_port = 7000
token = <TERRITORY_OWL_FRP_TOKEN>
```

`deploy/territory_owl/frpc.ini` must contain the same token and start with:

```ini
[common]
token = <TERRITORY_OWL_FRP_TOKEN>
server_addr = territory-owl-frps
server_port = 7000
admin_addr = 0.0.0.0
admin_port = 7400
```

Add one directory per challenge under `territory_owl_runtime/templates/`. Every
template must include a `docker-compose.yml` that joins the network named by
`TERRITORY_OWL_CONTAINERS_NETWORK` (default: `territory_owl_containers`). The
template receives the generated flag as `FLAG` in its `.env` file.

Set these private deployment variables in `.env`:

```dotenv
TERRITORY_OWL_PUBLIC_HOST=ctfd.example.org
TERRITORY_OWL_FRP_TOKEN=<random-secret>
TERRITORY_OWL_FRP_IMAGE=fatedier/frp:v0.38.0
TERRITORY_OWL_PORT_START=42100
TERRITORY_OWL_PORT_END=42199
TERRITORY_OWL_MAX_INSTANCES_PER_TEAM=1
TERRITORY_OWL_INSTANCE_TTL_SECONDS=3600
```

Start the overlay:

```sh
docker compose \
  -f docker-compose.yml \
  -f deploy/docker-compose.territory-owl.yml \
  up -d --build ctfd territory-owl-frps territory-owl-frpc territory-owl-cleanup
```

The cleanup service stops expired containers every minute. With the default
limit, each team may have one active instance; launching the same challenge
returns its existing instance.

## Physical attack protocol

The root emits the documented `PROTO-SERIAL.md` event:

```text
UUID_REQUEST session=1 node=aa:bb:cc:dd:ee:ff uuid=<32-hex-team-uuid> attack_points=42
```

The worker posts the node MAC, UUID, and attack points to CTFd. CTFd verifies the team's saved AP balance, deducts AP for every valid attempt, resolves the configured multiplier combat rule, records the event, and returns a serial action.

The worker sends only documented commands:

```text
color aa:bb:cc:dd:ee:ff #RRGGBB
reject aa:bb:cc:dd:ee:ff
```

A capture returns the attacking team's configured color. A defended attempt returns the current owner's color. Neutralization returns black. Unknown UUIDs, unknown nodes, insufficient AP, and worker/API errors are rejected.

## Combat and score

```text
remaining = old_defense * TERRITORY_DEFENSE_MULTIPLIER
            - attack_points * TERRITORY_ATTACK_MULTIPLIER
```

- Positive remaining defense keeps the current owner.
- Zero neutralizes the territory.
- Negative remaining captures it for the attacking team with `abs(remaining)` defense.
- Native CTFd awards are issued at each territory's configured interval while it has an owner.

Run focused tests before deployment:

```sh
python -m unittest discover -s tests -v
cd serial_worker && go test ./...
```
