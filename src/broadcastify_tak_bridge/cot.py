"""Cursor-on-Target XML builders and TAK Server transport."""

from __future__ import annotations

import logging
import queue
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


log = logging.getLogger(__name__)


def cot_time(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cot_stale(seconds: int) -> str:
    return cot_time(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def xml_text(value: object) -> str:
    return escape(str(value), {"\"": "&quot;", "'": "&apos;"})


def xml_attr(value: object) -> str:
    return quoteattr(str(value))


def build_geochat(
    sender_uid: str,
    sender_callsign: str,
    room_name: str,
    message: str,
) -> str:
    now = cot_time()
    stale = cot_stale(3600)
    message_uid = str(uuid.uuid4())
    event_uid = f"GeoChat.{sender_uid}.{room_name}.{message_uid}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<event version="2.0" uid={xml_attr(event_uid)} type="b-t-f" '
        f'how="h-e" time="{now}" start="{now}" stale="{stale}">\n'
        '  <point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>\n'
        '  <detail>\n'
        f'    <__chat id={xml_attr(room_name)} chatroom={xml_attr(room_name)} '
        f'groupOwner="false" messageId={xml_attr(message_uid)} '
        f'senderCallsign={xml_attr(sender_callsign)}>\n'
        f'      <chatgrp uid0={xml_attr(sender_uid)} uid1={xml_attr(room_name)} '
        f'id={xml_attr(room_name)}/>\n'
        '    </__chat>\n'
        f'    <link uid={xml_attr(sender_uid)} type="a-f-G-U-C" relation="p-p"/>\n'
        f'    <remarks source={xml_attr("BAO.F.ATAK." + sender_uid)} '
        f'time="{now}">{xml_text(message)}</remarks>\n'
        '  </detail>\n'
        '</event>'
    )


def build_marker(
    uid: str,
    cot_type: str,
    latitude: float,
    longitude: float,
    callsign: str,
    remarks: str,
    stale_seconds: int = 1800,
    color: str = "-256",
) -> str:
    now = cot_time()
    stale = cot_stale(stale_seconds)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<event version="2.0" uid={xml_attr(uid)} type={xml_attr(cot_type)} '
        f'how="h-g-i-g-o" time="{now}" start="{now}" stale="{stale}">\n'
        f'  <point lat="{latitude}" lon="{longitude}" hae="0" '
        'ce="999999" le="999999"/>\n'
        '  <detail>\n'
        f'    <contact callsign={xml_attr(callsign)}/>\n'
        f'    <color argb={xml_attr(color)}/>\n'
        f'    <remarks>{xml_text(remarks[:400])}</remarks>\n'
        '  </detail>\n'
        '</event>'
    )


class CotSender:
    """Send CoT XML to TAK Server on a persistent TLS connection."""

    def __init__(self, config: dict):
        self.host = config["host"]
        self.port = int(config.get("port", 8089))
        self.tls = bool(config.get("tls", True))
        self.cert = config.get("cert")
        self.key = config.get("key")
        self.ca = config.get("ca")
        self.verify_hostname = bool(config.get("verify_hostname", True))
        self.reconnect_seconds = float(config.get("reconnect_seconds", 5))
        self._queue: queue.Queue[str] = queue.Queue(
            maxsize=int(config.get("queue_size", 500))
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._publisher_loop,
            daemon=True,
            name="tak-publisher",
        )

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def validate(self) -> None:
        if not self.tls:
            return
        self.required_pem("client certificate", self.cert)
        self.required_pem("client private key", self.key)
        self.required_pem("TAK Server CA certificate", self.ca)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def send(self, cot_xml: str) -> bool:
        try:
            self._queue.put_nowait(cot_xml)
            return True
        except queue.Full:
            log.error("TAK output queue is full; the service dropped one event")
            return False

    @staticmethod
    def required_pem(label: str, value: str | None) -> Path:
        if not value:
            raise ValueError(f"Missing {label} path")
        path = Path(value)
        if path.suffix.lower() in {".p12", ".pfx"}:
            raise ValueError(f"{label} must be a PEM file")
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        return path

    def _connect(self) -> socket.socket:
        context = None
        if self.tls:
            cert = self.required_pem("client certificate", self.cert)
            key = self.required_pem("client private key", self.key)
            ca = self.required_pem("TAK Server CA certificate", self.ca)
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
            context.check_hostname = self.verify_hostname
            context.load_cert_chain(str(cert), str(key))

        raw = socket.create_connection((self.host, self.port), timeout=10)
        if context:
            connection = context.wrap_socket(raw, server_hostname=self.host)
        else:
            log.warning("TAK TLS is disabled; CoT events will use plaintext TCP")
            connection = raw
        connection.settimeout(None)
        log.info("Connected to TAK Server at %s:%s", self.host, self.port)
        return connection

    def _publisher_loop(self) -> None:
        connection: socket.socket | None = None
        pending: str | None = None
        while not self._stop.is_set():
            if connection is None:
                try:
                    connection = self._connect()
                    if pending:
                        connection.sendall(pending.encode("utf-8"))
                        pending = None
                except Exception as exc:
                    log.error("TAK connection failed: %s", exc)
                    self._stop.wait(self.reconnect_seconds)
                    continue

            try:
                pending = self._queue.get(timeout=1)
                connection.sendall(pending.encode("utf-8"))
                pending = None
            except queue.Empty:
                continue
            except Exception as exc:
                log.warning("TAK connection was lost: %s", exc)
                try:
                    connection.close()
                except OSError:
                    pass
                connection = None

        if connection:
            try:
                connection.close()
            except OSError:
                pass
