"""
cot_sender.py
Publishes CoT XML directly to TAK Server via TCP/SSL.
Dedicated publisher thread with reconnect handling.
"""
import ssl
import socket
import logging
import threading
import queue
import time
from pathlib import Path

log = logging.getLogger('cot_sender')

class COTSender:
    def __init__(self, config):
        cfg = config['tak_server']
        self.host = cfg.get('host', '127.0.0.1')
        self.port = cfg.get('port', 8089)
        self.use_ssl = cfg.get('ssl', True)
        self.cert = cfg.get('cert')       # path to client certificate PEM
        self.key = cfg.get('key')         # path to client private-key PEM
        self.ca = cfg.get('ca')           # path to TAK Server CA certificate PEM
        self.verify_hostname = cfg.get('verify_hostname', True)
        self.sender_uid = cfg.get('sender_uid', 'dispatch-feed')
        self._send_queue = queue.Queue(maxsize=500)
        self._thread = threading.Thread(
            target=self._publisher_loop,
            daemon=True,
            name='tak-publisher'
        )
        self._thread.start()

    def send(self, cot_xml):
        """Non-blocking — queues CoT for publisher thread."""
        try:
            self._send_queue.put_nowait(cot_xml)
            return True
        except queue.Full:
            log.warning("Send queue full — dropping message")
            return False

    def _connect(self):
        ctx = None
        if self.use_ssl:
            cert = self._required_pem('client certificate', self.cert)
            key = self._required_pem('client private key', self.key)
            ca = self._required_pem('TAK Server CA certificate', self.ca)
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
            ctx.check_hostname = self.verify_hostname
            ctx.load_cert_chain(str(cert), str(key))

        raw = socket.create_connection((self.host, self.port), timeout=10)
        if ctx:
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            sock = raw
        sock.settimeout(None)
        log.info(f"COTSender connected to {self.host}:{self.port} (ssl={self.use_ssl})")
        return sock

    @staticmethod
    def _required_pem(label, value):
        if not value:
            raise ValueError(f"Missing {label} path")
        path = Path(value)
        if path.suffix.lower() in {'.p12', '.pfx'}:
            raise ValueError(
                f"{label} must be PEM; convert PKCS#12 files before startup"
            )
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        return path

    def _publisher_loop(self):
        sock = None
        pending = None

        while True:
            if sock is None:
                try:
                    sock = self._connect()
                    if pending:
                        self._send_raw(sock, pending)
                        log.info("Retried in-flight message after reconnect")
                        pending = None
                except Exception as e:
                    log.error(f"TAK Server connect failed: {e} — retrying in 5s")
                    sock = None
                    time.sleep(5)
                    continue

            try:
                while True:
                    try:
                        cot_xml = self._send_queue.get(timeout=1)
                    except queue.Empty:
                        break
                    pending = cot_xml
                    self._send_raw(sock, cot_xml)
                    pending = None

            except Exception as e:
                log.warning(f"TAK Server connection lost: {e} — reconnecting")
                try:
                    sock.close()
                except:
                    pass
                sock = None

    def _send_raw(self, sock, cot_xml):
        data = cot_xml.encode('utf-8')
        sock.sendall(data)
