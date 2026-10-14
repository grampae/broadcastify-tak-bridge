"""Service entry point and health endpoint."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ConfigError, load_config
from .cot import CotSender
from .locations import LocationResolver
from .pipeline import BroadcastifyTakPipeline


log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = {
            "service": "broadcastify-tak-bridge",
            "state": "starting",
            "started_at": utc_now(),
            "last_success": None,
            "processed_transcripts": 0,
            "errors": 0,
            "last_error": "",
        }

    def update(
        self,
        state: str | None = None,
        success: bool = False,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if state:
                self._values["state"] = state
            if success:
                self._values["last_success"] = utc_now()
                self._values["processed_transcripts"] += 1
            if error:
                self._values["errors"] += 1
                self._values["last_error"] = error[:300]

    def snapshot(self, tak_queue_size: int = 0) -> dict:
        with self._lock:
            values = dict(self._values)
        values["tak_queue_size"] = tak_queue_size
        return values


class HealthServer:
    def __init__(self, host: str, port: int, status: ServiceStatus, sender: CotSender):
        self.status = status
        self.sender = sender
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path not in {"/", "/health"}:
                    self.send_error(404)
                    return
                body = json.dumps(
                    parent.status.snapshot(parent.sender.queue_size),
                    indent=2,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="health-server",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def run(config_path: str) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        log.error("Configuration error: %s", exc)
        return 2

    logging.basicConfig(
        level=getattr(logging, str(config["service"]["log_level"]).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    stop_event = threading.Event()
    status = ServiceStatus()
    sender = CotSender(config["tak_server"])
    health = None
    pipeline = None

    def request_stop(signum=None, frame=None) -> None:
        if not stop_event.is_set():
            log.info("Stopping service")
            status.update(state="stopping")
            stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        sender.validate()
        sender.start()
        port = int(config["service"].get("health_port", 8080))
        if port:
            health = HealthServer(
                str(config["service"].get("health_host", "127.0.0.1")),
                port,
                status,
                sender,
            )
            health.start()
            log.info("Health endpoint is listening on port %s", port)

        status.update(state="loading_model")
        resolver = LocationResolver(config["locations"])
        pipeline = BroadcastifyTakPipeline(
            config,
            sender,
            resolver,
            status.update,
            stop_event,
        )
        pipeline.start()
        log.info("Broadcastify TAK Bridge is running")
        while not stop_event.wait(1):
            pass
        return 0
    except Exception as exc:
        status.update(state="failed", error=str(exc))
        log.exception("Service failed")
        return 1
    finally:
        if pipeline:
            pipeline.stop()
        if sender.is_running:
            sender.stop()
        if health:
            health.stop()
        status.update(state="stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe Broadcastify audio and send it to TAK Server"
    )
    parser.add_argument(
        "--config",
        default="/app/config.yml",
        help="Path to the YAML configuration file",
    )
    args = parser.parse_args()
    sys.exit(run(args.config))
