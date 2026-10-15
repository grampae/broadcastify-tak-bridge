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
- Uses a local location file and an optional address database.
- Provides an optional external geocoding fallback.
- Provides a local JSON health endpoint.
- Keeps passwords outside the YAML configuration file.
- Hides transcript text from service logs by default.

## Requirements

- A Linux host with Docker Engine, Docker Compose, and OpenSSL.
- An authenticated audio stream that you have permission to use.
- An official TAK Server with a TLS input on port `8089`.
- A TAK client certificate, private key, and certificate-authority certificate.
- Sufficient memory and storage for the selected faster-whisper model.

See the [official TAK Server repository](https://github.com/TAK-Product-Center/Server) and its [CoreConfig example](https://github.com/TAK-Product-Center/Server/blob/main/src/takserver-core/example/CoreConfig.example.xml).

## Quick start

Clone the repository, then run the guided setup:

```bash
git clone https://github.com/grampae/broadcastify-tak-bridge.git
cd broadcastify-tak-bridge
./bridge setup
./bridge start
```

The setup asks for the stream, TAK Server, audio layout, model, and chat room. It creates the private configuration files, imports a `.p12` or `.pfx` certificate, and checks the completed installation. Existing local configuration is not replaced without confirmation.

If you already have PEM files, leave the certificate-bundle answer blank and put these files in `certs/`:

```text
certs/client.pem
certs/client.key
certs/ca.pem
```

Use a dedicated TAK client identity for this service. Do not use a TAK Server private key.

The first startup downloads the selected faster-whisper model and can take several minutes. Follow its progress with:

```bash
./bridge logs
```

## Manage the service

| Command | Purpose |
|---|---|
| `./bridge doctor` | Check Docker, configuration, certificates, and TAK connectivity. |
| `./bridge start` | Validate, build, and start the service. |
| `./bridge status` | Show the container state and local health response. |
| `./bridge logs` | Follow the most recent service logs. |
| `./bridge stop` | Stop the service. |
| `./bridge update` | Fast-forward the Git checkout and rebuild the image. |

Run `./bridge setup` again to replace `.env` and `config.yml`. The command asks for confirmation first and preserves an existing `locations.yml`.

## Optional location markers

GeoChat works without location data. A new guided installation creates an empty `locations.yml`; add private locations only when you want the bridge to create markers:

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

The resolver uses the longest matching name or alias. The `unit_map` corrects common spoken unit names after transcription. You can also provide a SQLite address database; see [data/README.md](data/README.md) for its schema.

External geocoding is disabled by default. When enabled in `config.yml`, the service sends detected address text to the configured public geocoder. Do not enable it when dispatch text must remain inside your deployment.

## Advanced configuration

The guided setup puts common values in `.env` and operational options in `config.yml`. Docker Compose passes the environment values to the service, which replaces matching `${VARIABLE}` references in the YAML file.

- Change the faster-whisper model, TAK room, and sender callsign in `.env`.
- Change confidence, chunk duration, TLS behavior, marker lifetime, and geocoding in `config.yml`.
- Keep `verify_hostname: true` when the TAK hostname matches the server certificate. If a deployment must use an unmatched IP address, set it to `false`; certificate-authority validation remains enabled.
- Each configured left or right channel opens one connection to the audio stream. Use a single `mono` channel unless the source is known to carry separate traffic on each stereo channel.
- Keep `HF_HUB_OFFLINE=0` until the first model download completes. It can then be set to `1` for offline operation.

For a completely manual installation, copy `.env.example`, `config.example.yml`, and `locations.example.yml` to their names without `.example`, place the three PEM files in `certs/`, and run `./bridge doctor`.

## Verify the deployment

Check the container and health endpoint:

```bash
./bridge status
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
3. If location markers are configured, speak or wait for a configured alias and confirm that ATAK receives its marker.

## Configuration reference

| Section | Purpose |
|---|---|
| `service` | Controls logging and the health endpoint. |
| `tak_server` | Controls the mutual-TLS connection to TAK Server. |
| `broadcastify` | Controls the stream credentials, channels, and audio chunks. |
| `whisper` | Controls the faster-whisper model and transcript threshold. |
| `tak_output` | Controls the GeoChat identity, room, and marker lifetime. |
| `locations` | Controls location data and optional geocoding. |

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
- `locations.address_database` points to the correct database when you use exact address matching.

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
