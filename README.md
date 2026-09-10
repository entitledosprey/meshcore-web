# meshcore-web

A web console for managing [MeshCore](https://meshcore.co.uk/) repeaters from a
USB companion radio. Runs as a container on a Raspberry Pi with the radio
attached over USB.

![license](https://img.shields.io/badge/license-MIT-blue)

## What it does

- **Dashboard** — node identity, radio settings, battery and temperature, and a
  live event stream from the radio.
- **Chat** — channel messaging (defaults to `#public`) and direct messages, with
  history that survives restarts. Add channels with an optional shared key.
- **Contacts** — every known node, filterable by type, with path discovery,
  contact URI export and removal.
- **Repeaters** — status, neighbours, telemetry and remote commands against
  repeaters, with saved passwords and automatic login.
- **Console** — the full `meshcore-cli` command set in the browser.

Everything is served from one process that owns the serial port, so the radio
never sees two clients competing for it.

## Requirements

- A MeshCore companion radio on USB (developed against a Heltec V3/V4).
- Docker with the Compose plugin.
- Linux host. The device is passed into the container, so the port must exist
  on the host.

## Install

Give the radio a stable device name so a replug cannot renumber it:

```sh
# adjust idVendor/idProduct/serial to match your radio:
#   udevadm info -a -n /dev/ttyACM0 | grep -m3 -E 'idVendor|idProduct|serial'
sudo tee /etc/udev/rules.d/99-meshcore.rules <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", SYMLINK+="meshcore", GROUP="dialout", MODE="0660"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

Then start it:

```sh
git clone https://github.com/entitledosprey/meshcore-web.git
cd meshcore-web
docker compose up -d
```

The console is on <http://localhost:8080>.

To build locally instead of pulling the published image:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MESH_PORT` | `/dev/ttyACM0` | Serial device inside the container |
| `MESH_BAUD` | `115200` | Serial baud rate |
| `MESH_PASSWORD_FILE` | `/data/passwords.json` | Saved repeater passwords |
| `MESH_MESSAGE_FILE` | `/data/messages.json` | Chat history |
| `MESH_LOGIN_TTL` | `900` | Seconds a repeater login is reused |
| `LOG_LEVEL` | `INFO` | Python log level |

State lives in the `meshcore-data` volume.

## HTTPS

`docker-compose.tls.yml` adds a Caddy front end that obtains a certificate
through the Cloudflare DNS-01 challenge, so the host never needs to be reachable
from the internet. Set `CF_API_TOKEN` and `MESH_HOSTNAME` in a `.env` file and
bring it up alongside the base compose file:

```sh
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

The API token needs `Zone:DNS:Edit` on the zone only.

The same file gates the site behind HTTP basic auth. Generate a hash and note
that Compose reads `$` as interpolation, so each one must be doubled in `.env`:

```sh
docker run --rm ghcr.io/caddybuilds/caddy-cloudflare:2.11.4 \
  caddy hash-password --plaintext 'your-password' | sed 's/\$/$$/g'
``` Point an A record for
the hostname at the host's LAN address, DNS-only (not proxied) — the DNS-01
challenge proves ownership without the host being reachable from outside.

## Security

There is **no authentication** on the web interface. Anyone who can reach the
port can send adverts, reboot repeaters, read your messages and use any repeater
password you have saved. Repeater passwords and chat history are stored
unencrypted in the volume — the radio protocol needs the password itself, so
hashing it would not work. Keep this on a trusted network.

## Notes

Only one process can hold the serial port. While the container is running,
`meshcli` on the host cannot open the radio; stop the container first or use the
built-in console.

## License

MIT
