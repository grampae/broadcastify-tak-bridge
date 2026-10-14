# TAK Feed Manager

TAK Feed Manager converts public-safety and situational-awareness feeds into Cursor-on-Target (CoT) events. It sends the events directly to an official TAK Server through mutual TLS.

The Broadcastify feed uses FFmpeg and faster-whisper. It sends dispatch transcripts as ATAK GeoChat messages. It also creates map markers when it can resolve a location.

## Supported feeds

| Feed | Input | TAK output |
|---|---|---|
| Broadcastify | Authenticated stereo audio stream | GeoChat messages and incident markers |
| NWS | Weather alerts | Weather markers |
| NIFC | Wildfire perimeter service | Wildfire markers |
| APRS | APRS-IS | Station markers |
| ADS-B | OpenSky or local dump1090/readsb | Aircraft markers |
| AIS | AISstream.io | Vessel markers |
| S2 Underground | ArcGIS feature service | Incident markers |

## Broadcastify data flow

```text
Broadcastify audio
        |
        v
curl -> FFmpeg -> 16 kHz PCM -> faster-whisper
                                  |
                                  v
                    transcript filters and classification
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
              ATAK GeoChat             location resolver
                                                |
                                                v
                                          CoT marker
                    \___________________________/
                                  |
                                  v
                       TAK Server TLS port 8089
```

The location resolver checks the local address database first. It then checks the configured towns, roads, landmarks, and aliases. Nominatim is the final fallback.

## Requirements

- A Linux host with Docker Engine and Docker Compose.
- An official TAK Server that accepts TLS clients on port `8089`.
- A TAK client certificate, its private key, and the TAK certificate-authority certificate in PEM format.
- Broadcastify stream credentials for each enabled Broadcastify feed.
- Internet access for each enabled online feed.
- Sufficient memory and storage for the selected faster-whisper model.

The official TAK Server example enables its standard TLS input on port `8089`. See the [official CoreConfig example](https://github.com/TAK-Product-Center/Server/blob/main/src/takserver-core/example/CoreConfig.example.xml) and the [official TAK Server repository](https://github.com/TAK-Product-Center/Server).

## Protect credentials

**Warning:** Do not commit `feeds.yml`, `.env`, private keys, PKCS#12 files, production certificates, logs, or generated databases. A public commit exposes every secret in its Git history.

The repository ignores these files. Keep the deployed files outside Git backups too.

## Install

### 1. Create the local configuration

Copy the example file:

```bash
cp feeds.example.yml feeds.yml
chmod 600 feeds.yml
```

Set these values in `feeds.yml`:

- `tak_server.host`
- The paths under `tak_server.cert`, `tak_server.key`, and `tak_server.ca`
- The area boundaries and enabled feeds
- The credentials for each enabled feed

Keep all credential-based feeds disabled until their values are valid.

### 2. Install the TAK client certificates

Use a dedicated TAK client identity for this service. Do not use a TAK Server private key.

If TAK Server generated PEM files, copy the client certificate, client key, and CA certificate into `certs/`:

```text
certs/client.pem
certs/client.key
certs/ca.pem
```

If you have a PKCS#12 client package, convert it before startup:

```bash
openssl pkcs12 -in client.p12 -clcerts -nokeys -out certs/client.pem
openssl pkcs12 -in client.p12 -nocerts -nodes -out certs/client.key
openssl pkcs12 -in client.p12 -cacerts -nokeys -out certs/ca.pem
chmod 600 certs/client.key
```

Some PKCS#12 packages do not contain the CA certificate. In that case, get `ca.pem` from the TAK Server administrator.

The sender does not load `.p12` or `.pfx` files directly.

Verify the certificate chain:

```bash
openssl verify -CAfile certs/ca.pem certs/client.pem
```

Test the TAK Server connection:

```bash
openssl s_client \
  -connect tak.example.com:8089 \
  -cert certs/client.pem \
  -key certs/client.key \
  -CAfile certs/ca.pem \
  -verify_return_error </dev/null
```

Replace `tak.example.com` with the configured TAK Server name.

Set `verify_hostname: true` when the server name matches the certificate. If you must connect by an unmatched IP address, set it to `false`. The sender still verifies the certificate chain against `ca.pem`.

### 3. Configure Broadcastify

Each Broadcastify instance is a separate item under `feeds`:

```yaml
broadcastify_example:
  type: broadcastify
  enabled: true
  stream_url: "https://example.invalid/stream"
  premium_username: "YOUR_USERNAME"
  premium_password: "YOUR_PASSWORD"
  chat_room: "DISPATCH"
  whisper_model: "medium.en"
  min_confidence: 0.40
  locations_file: /app/locations_marquette.yml
  address_db: /app/data/marquette_addresses.db
  geocode_suffix: "Marquette County, Michigan, USA"
  sender_uid: dispatch-feed-example
  sender_callsign: DISPATCH
```

The current capture path expects stereo audio. The left and right channels run as separate transcription workers.

### 4. Customize location data

Edit a location YAML file for each coverage area. Each file can define towns, landmarks, roads, aliases, and spoken unit names.

```yaml
towns:
  example town: [46.5000, -87.5000, ["example township"]]

landmarks:
  example hospital: [46.5100, -87.4900, ["the hospital"]]

roads:
  route-1: [46.5000, -87.5000, ["route 1", "highway 1"]]

unit_map:
  engine one: Engine 1
```

Set `locations_file` to the container path for that file. Add its read-only mount to `docker-compose.yml` when you create a new file.

### 5. Build the optional address databases

The repository does not include generated SQLite databases. Build the databases that your enabled feeds use:

```bash
docker compose build

docker compose run --rm manpack-feeds \
  python3 build_address_db.py --service marquette \
  --output /app/data/marquette_addresses.db

docker compose run --rm manpack-feeds \
  python3 build_address_db.py --service ri \
  --output /app/data/ri_addresses.db
```

The build script downloads public address features from the configured ArcGIS services.

### 6. Download the Whisper model

The first Broadcastify startup downloads the configured faster-whisper model into `hf_cache/`. Keep `HF_HUB_OFFLINE=0` for the first startup.

To download the model before service startup, run:

```bash
docker compose run --rm manpack-feeds python3 -c \
  "from faster_whisper import WhisperModel; WhisperModel('medium.en', device='cpu', compute_type='int8')"
```

After the download succeeds, set this value in a local `.env` file when the production host has no internet access:

```dotenv
HF_HUB_OFFLINE=1
```

### 7. Start the service

Validate and start the deployment:

```bash
docker compose config -q
docker compose up -d --build
docker compose logs --tail=100 -f manpack-feeds
```

A successful TAK connection produces a message similar to this one:

```text
COTSender connected to tak.example.com:8089 (ssl=True)
```

## Verify operation

Use these checks after startup:

1. Open ATAK and connect it to the same TAK Server.
2. Confirm that enabled position feeds create map markers.
3. Confirm that Broadcastify transcripts appear in the configured GeoChat room.
4. Confirm that a transcript with a resolved location creates a CoT marker.
5. Check the local health endpoint.

```bash
curl http://127.0.0.1:8080/health
```

The dashboard listens on the configured `netbird_ip`. It uses `127.0.0.1` when that setting is absent.

## Configuration notes

- `stale_times` controls how long ATAK retains each marker type.
- `min_confidence` rejects Broadcastify transcripts below the configured score.
- `address_db` selects the local SQLite database for exact address lookup.
- `geocode_suffix` restricts fallback geocoding to the intended region.
- `sender_uid` must be stable and unique for each Broadcastify instance.
- `verify_hostname` controls TLS hostname validation. It does not disable CA validation.
- `--feeds` starts only the specified configured feed names.

For example:

```bash
docker compose run --rm manpack-feeds \
  python3 -u feed_manager.py --config /app/feeds.yml \
  --feeds broadcastify_marquette weather
```

## Troubleshooting

### TAK Server rejects the connection

Confirm the following items:

- Port `8089` is reachable from the container host.
- `client.pem` is a client certificate from the correct TAK certificate authority.
- `client.key` matches `client.pem`.
- `ca.pem` validates the TAK Server certificate.
- The client certificate is valid and is not revoked.
- The system clocks on both hosts are correct.

### Whisper cannot load the model

Set `HF_HUB_OFFLINE=0` and start the service once with internet access. Confirm that `hf_cache/` contains the model before you enable offline mode.

### GeoChat works but no incident marker appears

The transcript did not resolve to a location. Confirm these items:

- The configured location YAML file contains the spoken place name or an alias.
- The configured address database exists in `data/`.
- `address_db` points to the correct database.
- `geocode_suffix` identifies the correct region.

### No Broadcastify transcript appears

Confirm the stream URL and credentials. Then inspect the logs for FFmpeg, stream, or Whisper errors.

## Test

Create a Python environment and run the unit tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests parse generated CoT as XML and check the certificate-path validation logic.

## Repository data policy

The repository includes the location YAML files because they define application behavior. It excludes these operational artifacts:

- `feeds.yml`
- `certs/`
- `data/*.db`
- `logs/`
- `hf_cache/`
- Python bytecode and test caches

Review local location data before publication. Remove any private address or person-specific alias that is not necessary.

## Stop the service

```bash
docker compose down
```
