# Root Serial Protocol

This document describes the line-oriented USB serial interface exposed by `root` firmware. It carries mesh events from the root to the host and commands from the host to the root.

## Transport And Framing

- Use the ESP-IDF console/USB serial transport configured for the root firmware.
- Commands and events are UTF-8-compatible ASCII text lines terminated by `LF`, `CR`, or `CRLF`.
- The root removes the line terminator before parsing.
- Input is read into a 160-byte buffer. Keep a complete command line to 159 characters or fewer, including the command, spaces, and arguments.
- Event fields are separated by single spaces. MAC addresses are lowercase hexadecimal in root output; parsers should accept either case.

On startup, the root prints the command help text. Informational ESP-IDF logs can appear on the same serial stream, so host software should select protocol lines by their prefixes.

## Asynchronous Events

### `NODE_REPORT`

Emitted for every valid nearest-advertiser report received from a node:

```text
NODE_REPORT session=<u32> node=<mesh-mac> ble=<ble-address> rssi=<i8> distance_cm=<u16> payload_hex=<hex>
```

Example:

```text
NODE_REPORT session=123456 node=aa:bb:cc:dd:ee:ff ble=11:22:33:44:55:66 rssi=-52 distance_cm=78 payload_hex=020106
```

`payload_hex` is the raw BLE advertising payload seen by the node, hex encoded without separators. The root records this node and session as the target of a later `reply` command, then automatically sends the configured default response (`CONFIG_SS_ROOT_DEFAULT_RESPONSE`) to that node.

### `UUID_REQUEST`

Emitted when a node forwards a BLE GATT team request:

```text
UUID_REQUEST session=<u32> node=<mesh-mac> uuid=<32-lowercase-hex> attack_points=<u16> uuid_hex=<32-lowercase-hex>
```

Example:

```text
UUID_REQUEST session=123456 node=aa:bb:cc:dd:ee:ff uuid=550e8400e29b41d4a716446655440000 attack_points=42 uuid_hex=550e8400e29b41d4a716446655440000
```

`uuid` and `uuid_hex` are currently identical: the 16 raw request bytes, hex encoded without separators or UUID dashes. The root stores one pending UUID request per node for 30 seconds. A newer UUID request from the same node replaces its previous pending request. At most 16 pending requests are retained; an event is still printed if the pending table is full, but it cannot later be approved or rejected through `color` or `reject`.

### Topology Output

`routes` produces:

```text
ROUTES count=<decimal>
<index> <mesh-mac>
...
```

`tree` produces:

```text
TREE count=<decimal> complete=<0-or-1>
<index> <mesh-mac> parent=<mesh-mac> parent_known=<0-or-1> direct_child=<0-or-1>
...
```

Failures are reported as `ERR routes <esp_err_name>` or `ERR tree <esp_err_name>`.

## Commands

Except for `ping`, command names are lowercase and case-sensitive. Arguments are separated by literal spaces. A mesh MAC is six two-digit hexadecimal octets separated by colons. An RGB color is exactly six hexadecimal digits, optionally prefixed by `#`.

| Command | Effect |
| --- | --- |
| `help` | Print the command list. |
| `ping` | Print `PONG`. This command is case-insensitive. |
| `routes` | Print the ESP-MESH routing table. |
| `tree` | Print the current mesh-tree snapshot. |
| `reply <text>` | Send a text root response to the node from the most recent `NODE_REPORT`. |
| `send <node_mac> <text>` | Send a text root response to a specific node with session ID 0. |
| `color <node_mac> <#RRGGBB>` | Approve and consume that node's pending UUID request, sending its requested-session RGB response. |
| `reject <node_mac>` | Reject and consume that node's pending UUID request with `INSUFFICIENT_ATTACK_POINTS`. |
| `sendcolor <node_mac> <#RRGGBB>` | Send an immediate RGB update to a node with session ID 0. |

`reply` returns `ERR no pending node` until at least one `NODE_REPORT` is received. It does not consume the remembered report and may be used repeatedly.

`color` and `reject` require an unexpired pending `UUID_REQUEST` for the exact node MAC. On success they print one of:

```text
COLOR_SENT node=<mesh-mac> session=<u32> color=<rrggbb>
UUID_REJECTED node=<mesh-mac> session=<u32>
```

## Responses And Errors

The root recognizes these command errors:

```text
ERR unknown command
ERR no pending node
ERR usage: send <node_mac> <text>
ERR usage: color <node_mac> <#RRGGBB>
ERR usage: reject <node_mac>
ERR usage: sendcolor <node_mac> <#RRGGBB>
ERR invalid mac
ERR invalid mac or color
ERR invalid color
ERR no pending UUID request for <mesh-mac>
```

Text supplied to `reply` or `send` is copied into a mesh response payload and silently truncated to 64 bytes. RGB payloads are three raw bytes in red, green, blue order. A response with session ID 0 is an out-of-band message: nodes accept it without matching it to their current scan or UUID session. In particular, `sendcolor` immediately updates a node's retained color.

The serial command parser confirms local parsing and queue state, not mesh delivery. Mesh-send failures are reported through ESP-IDF log output rather than a structured serial result line.
