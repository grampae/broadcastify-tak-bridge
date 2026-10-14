# Broadcastify TAK Bridge

Broadcastify TAK Bridge is a standalone service for ATAK deployments. It converts an authenticated Broadcastify audio stream into TAK GeoChat messages and optional Cursor-on-Target (CoT) markers.

The service uses faster-whisper for speech recognition. It connects directly to an official TAK Server through mutual TLS on the standard CoT input port.

This project is a server-side bridge. It is not an Android plugin, and it does not require an ATAK application package.

## Data flow

```text
Broadcastify audio stream
          |
          v
       FFmpeg
          |
          v
16 kHz mono PCM chunks
          |
          v
   faster-whisper
          |
          v
 transcript filtering
          |
          +----------------------+
          |                      |
          v                      v
    TAK GeoChat          optional location lookup
                                 |
                                 v
                           TAK CoT marker
          \______________________/
                     |
                     v
          TAK Server mutual TLS
                     |
                     v
                    ATAK
```

## Main features

- Transcribes mono or stereo Broadcastify audio.
- Supports separate labels for the left and right audio channels.
- Sends transcripts to a configured ATAK GeoChat room.
- Creates incident markers when a transcript contains a known location.
- Uses a local location file and an optional private address database.
- Provides an optional external geocoding fallback.
- Provides a local JSON health endpoint.
- Keeps passwords outside the YAML configuration file.
- Hides transcript text from service logs by default.

## Requirements

- A Linux host with Docker Engine and Docker Compose.
- An authenticated audio stream that you have permission to use.
- An official TAK Server with a TLS input on port `8089`.
- A TAK client certificate, private key, and certificate-authority certificate.
- Sufficient memory and storage for the selected faster-whisper model.

See the [official TAK Server repository](https://github.com/TAK-Product-Center/Server) and its [CoreConfig example](https://github.com/TAK-Product-Center/Server/blob/main/src/takserver-core/example/CoreConfig.example.xml).

## Protect private data

**Warning:** Do not commit `.env`, `config.yml`, `locations.yml`, production certificates, private keys, model caches, transcripts, or address databases.

Location aliases, feed URLs, talk-group details, and transcription prompts can identify an operating area. Review all files before you grant repository access.

The repository contains only fictional location examples. Git ignores the local deployment files.

## Install the service

### 1. Create the local files

Copy the example files:

```bash
cp .env.example .env
cp config.example.yml config.yml
cp locations.example.yml locations.yml
chmod 600 .env config.yml locations.yml
```

Edit `.env` and set these values:

```dotenv
BROADCASTIFY_STREAM_URL=https://your-authorized-audio-stream
BROADCASTIFY_USERNAME=your-account-name
BROADCASTIFY_PASSWORD=your-account-password
TAK_HOST=tak.example.com
HF_HUB_OFFLINE=0
```

Docker Compose passes these values to the service. The service replaces matching `${VARIABLE}` references in `config.yml`.

### 2. Install the TAK client certificates

Use a dedicated TAK client identity for this service. Do not use a TAK Server private key.

Put these PEM files in `certs/`:

```text
certs/client.pem
certs/client.key
certs/ca.pem
```

If you received a PKCS#12 file, convert it:

```bash
openssl pkcs12 -in client.p12 -clcerts -nokeys -out certs/client.pem
openssl pkcs12 -in client.p12 -nocerts -nodes -out certs/client.key
openssl pkcs12 -in client.p12 -cacerts -nokeys -out certs/ca.pem
chmod 600 certs/client.key
```

Some PKCS#12 files do not include the CA certificate. Get `ca.pem` from the TAK Server administrator when it is absent.

Verify the client certificate:

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

Replace `tak.example.com` with your TAK Server name.

Keep `verify_hostname: true` when the configured name matches the server certificate. If the deployment uses an unmatched IP address, set it to `false`. The service continues to validate the certificate authority.

### 3. Select the audio channels

The example configuration processes a stereo stream as two channels:

```yaml
broadcastify:
  channels:
    - source: left
      label: DISPATCH-L
    - source: right
      label: DISPATCH-R
```

Use one `mono` channel for a mono stream:

```yaml
broadcastify:
  channels:
    - source: mono
      label: DISPATCH
```

Each configured channel creates one audio-stream connection.

### 4. Configure faster-whisper

Set the model and recognition options in `config.yml`:

```yaml
whisper:
  model: medium.en
  device: cpu
  compute_type: int8
  language: en
  min_confidence: 0.35
  initial_prompt: >-
    Public safety radio dispatch. Engine, ladder, medic, police, fire, and EMS.
```

Use `initial_prompt` for common agency terms, unit names, and road names. Keep the completed prompt private if it identifies your coverage area.

The first startup downloads the selected model into `models/`. Keep `HF_HUB_OFFLINE=0` until the download finishes. You can then set `HF_HUB_OFFLINE=1` for an offline deployment.

### 5. Configure location markers

GeoChat does not require location data. The service sends a marker only when it resolves a location.

Edit `locations.yml`:

```yaml
locations:
  - name: Example Fire Station
    latitude: 40.0000
    longitude: -100.0000
    aliases:
      - central station

unit_map:
  engine one: Engine 1
```

The resolver uses the longest matching name or alias. The `unit_map` corrects common spoken unit names after transcription.

You can also provide a private SQLite address database. See [data/README.md](data/README.md) for its schema.

External geocoding is disabled by default. When enabled, the service sends detected address text to the configured public geocoder. Do not enable this function when dispatch text must remain inside your deployment.

### 6. Configure the TAK output

Set a stable sender identity and a GeoChat room:

```yaml
tak_output:
  chat_room: DISPATCH
  sender_uid: broadcastify-bridge
  sender_callsign: DISPATCH
  send_markers: true
  marker_stale_seconds: 1800
```

ATAK clients connected to the same TAK Server receive the GeoChat messages and markers. No ATAK-side installation is necessary.

### 7. Start the service

Validate the Compose file and start the container:

```bash
docker compose config -q
docker compose up -d --build
docker compose logs --tail=100 -f broadcastify-tak-bridge
```

The first model download can take several minutes.

## Verify the deployment

Check the health endpoint:

```bash
curl http://127.0.0.1:8080/health
```

A running service returns data similar to this example:

```json
{
  "service": "broadcastify-tak-bridge",
  "state": "running",
  "last_success": null,
  "processed_transcripts": 0,
  "errors": 0,
  "last_error": "",
  "tak_queue_size": 0
}
```

Then complete these checks:

1. Connect ATAK to the configured TAK Server.
2. Confirm that the configured GeoChat room receives a transcript.
3. Speak or wait for a configured location alias.
4. Confirm that ATAK receives a marker for that location.

## Configuration reference

| Section | Purpose |
|---|---|
| `service` | Controls logging and the health endpoint. |
| `tak_server` | Controls the mutual-TLS connection to TAK Server. |
| `broadcastify` | Controls the stream credentials, channels, and audio chunks. |
| `whisper` | Controls the faster-whisper model and transcript threshold. |
| `tak_output` | Controls the GeoChat identity, room, and marker lifetime. |
| `locations` | Controls private location data and optional geocoding. |

Set `service.log_transcripts: true` only during controlled troubleshooting. Transcript text can contain personal or operational information.

## Troubleshooting

### TAK Server rejects the connection

Confirm these items:

- Port `8089` is reachable from the Docker host.
- `client.pem` is a valid client certificate.
- `client.key` matches `client.pem`.
- `ca.pem` validates the TAK Server certificate.
- The client certificate is not expired or revoked.
- The clocks on both hosts are correct.

### No audio reaches faster-whisper

Confirm the stream URL and credentials. Then inspect the container logs for an HTTP or FFmpeg error.

For a stereo stream, confirm that each configured source is `left` or `right`. For a mono stream, configure only `mono`.

### GeoChat works but markers do not appear

Confirm these items:

- `tak_output.send_markers` is `true`.
- `locations.yml` contains the spoken name or an alias.
- The latitude and longitude are valid.
- `locations.address_database` points to the correct private database when you use exact address matching.

### The model does not load

Set `HF_HUB_OFFLINE=0` and restart the service with internet access. After the model appears in `models/`, you can enable offline mode.

## Development

Create a virtual environment and install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The documentation is STE-aligned. Controlled-dictionary verification is still required for formal ASD-STE100 conformance.

## Stop the service

```bash
docker compose down
```

## License status

This private project does not grant a public software license. Select a license before any public release.
