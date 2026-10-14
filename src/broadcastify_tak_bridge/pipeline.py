"""Broadcastify capture, transcription, filtering, and CoT conversion."""

from __future__ import annotations

import logging
import math
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import requests

from .cot import build_geochat, build_marker
from .locations import LocationResolver


log = logging.getLogger(__name__)

StatusCallback = Callable[..., None]

SKIP_PATTERNS = [
    re.compile(value, re.IGNORECASE)
    for value in (
        r"^\s*10[-\s]?4\.?\s*$",
        r"^\s*copy\.?\s*$",
        r"^\s*clear\.?\s*$",
        r"^\s*(dispatch|county),?\s*(go ahead|copy|clear|10-4)\.?\s*$",
        r"^\s*(radio check|signal check)\.?\s*$",
        r"^\s*(affirmative|negative|roger|wilco)\.?\s*$",
        r"^\s*(thank you|go ahead)\.?\s*$",
    )
]

HALLUCINATION_PATTERNS = [
    re.compile(value, re.IGNORECASE)
    for value in (
        r"^\s*\[.*?\]\s*$",
        r"^\s*\(.*?\)\s*$",
        r"^\s*sound(s)? (of|effect)",
        r"^\s*transcript(ion)? (by|from)",
        r"^\s*subtitles? (by|from)",
        r"^\s*www\.",
        r"thank you for watching",
        r"please (like|subscribe)",
        r"^\s*[.!*]+\s*$",
        r"^\s*(uh+|um+|mm+|hmm+)\s*\.?\s*$",
    )
]

INCIDENT_PATTERNS = {
    "FIRE": ("-23296", ["structure fire", "building fire", "vehicle fire", "smoke"]),
    "EMS": ("-16711936", ["medical", "ambulance", "cardiac", "unresponsive", "overdose"]),
    "POLICE": ("-16776961", ["shots fired", "pursuit", "armed", "assault", "weapon"]),
    "ACCIDENT": ("-256", ["accident", "collision", "crash", "mva", "mvc"]),
    "HAZMAT": ("-16711936", ["hazmat", "chemical", "spill", "gas leak"]),
    "SAR": ("-256", ["missing", "search and rescue", "lost person"]),
    "EMERGENCY": ("-65536", ["mayday", "officer down", "10-33"]),
}


def clean_transcript(text: str, unit_map: dict[str, str] | None = None) -> str:
    value = text.strip()
    if not value or any(pattern.search(value) for pattern in HALLUCINATION_PATTERNS):
        return ""

    words = value.lower().split()
    for size in range(2, min(6, len(words) // 3 + 1)):
        phrases = [tuple(words[index : index + size]) for index in range(len(words) - size + 1)]
        if any(phrases.count(phrase) >= 3 for phrase in set(phrases)):
            return ""

    value = re.sub(
        r"\b(beep+|boop+|ding|dong|alert tone|tone out)\b",
        "[tone]",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s*(?:\.{3}|-+|,)$", "", value).strip()
    if unit_map:
        for spoken, canonical in unit_map.items():
            value = re.sub(
                r"\b" + re.escape(spoken) + r"\b",
                canonical,
                value,
                flags=re.IGNORECASE,
            )
    return value


def should_send(transcript: str) -> bool:
    if len(transcript.split()) < 5:
        return False
    return not any(pattern.match(transcript) for pattern in SKIP_PATTERNS)


def classify_incident(transcript: str) -> tuple[str, str]:
    lowered = transcript.lower()
    for label, (color, keywords) in INCIDENT_PATTERNS.items():
        if any(keyword in lowered for keyword in keywords):
            return label, color
    return "DISPATCH", "-256"


class WhisperTranscriber:
    def __init__(self, config: dict):
        from faster_whisper import WhisperModel

        self.language = config.get("language", "en")
        self.initial_prompt = config.get("initial_prompt", "")
        self._lock = threading.Lock()
        log.info(
            "Loading faster-whisper model %s on %s",
            config["model"],
            config.get("device", "cpu"),
        )
        self.model = WhisperModel(
            config["model"],
            device=config.get("device", "cpu"),
            compute_type=config.get("compute_type", "int8"),
        )

    def transcribe(self, pcm_bytes: bytes) -> tuple[str, float]:
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            segments, _ = self.model.transcribe(
                audio,
                language=self.language,
                beam_size=5,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 400},
                condition_on_previous_text=False,
                initial_prompt=self.initial_prompt or None,
            )
            values = [(segment.text.strip(), segment.avg_logprob) for segment in segments]

        values = [(text, probability) for text, probability in values if text]
        if not values:
            return "", 0.0
        average_log_probability = sum(item[1] for item in values) / len(values)
        confidence = max(0.0, min(1.0, math.exp(average_log_probability)))
        return " ".join(item[0] for item in values), confidence


class ChannelCapture:
    """Stream one audio channel and return fixed-duration PCM chunks."""

    SAMPLE_RATE = 16000
    SAMPLE_BYTES = 2

    def __init__(
        self,
        config: dict,
        stream_config: dict,
        stop_event: threading.Event,
        status_callback: StatusCallback,
    ):
        self.source = config["source"]
        self.label = config["label"]
        self.url = stream_config["stream_url"]
        self.username = stream_config.get("username", "")
        self.password = stream_config.get("password", "")
        self.chunk_bytes = (
            self.SAMPLE_RATE
            * self.SAMPLE_BYTES
            * int(stream_config.get("chunk_seconds", 10))
        )
        self.reconnect_max = int(stream_config.get("reconnect_max_seconds", 120))
        self.stop_event = stop_event
        self.status_callback = status_callback
        self.chunks: queue.Queue[bytes] = queue.Queue(maxsize=30)
        self._response: requests.Response | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"capture-{self.source}",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._response:
            self._response.close()
        if self._ffmpeg:
            self._ffmpeg.terminate()
        self._thread.join(timeout=5)

    def get_chunk(self, timeout: float = 1) -> bytes | None:
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    def _capture_loop(self) -> None:
        delay = 5
        while not self.stop_event.is_set():
            try:
                self._capture_once()
                delay = 5
            except Exception as exc:
                if not self.stop_event.is_set():
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        detail = f"HTTP {exc.response.status_code}"
                    elif isinstance(exc, requests.RequestException):
                        detail = type(exc).__name__
                    else:
                        detail = str(exc)
                    log.error("Audio channel %s failed: %s", self.label, detail)
                    self.status_callback(error=f"Audio channel {self.label}: {detail}")
            if not self.stop_event.is_set():
                log.info("Audio channel %s will reconnect in %s seconds", self.label, delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2, self.reconnect_max)

    def _capture_once(self) -> None:
        auth = None
        if self.username or self.password:
            auth = (self.username, self.password)
        log.info("Connecting Broadcastify audio channel %s", self.label)
        with requests.get(
            self.url,
            auth=auth,
            stream=True,
            timeout=(10, 60),
        ) as response:
            response.raise_for_status()
            self._response = response
            pan = {
                "mono": "pan=mono|c0=c0",
                "left": "pan=mono|c0=c0",
                "right": "pan=mono|c0=c1",
            }[self.source]
            self._ffmpeg = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-af",
                    pan,
                    "-ar",
                    str(self.SAMPLE_RATE),
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            writer = threading.Thread(
                target=self._write_stream,
                args=(response, self._ffmpeg),
                daemon=True,
                name=f"stream-{self.source}",
            )
            writer.start()
            buffer = bytearray()
            assert self._ffmpeg.stdout is not None
            while not self.stop_event.is_set():
                data = self._ffmpeg.stdout.read(4096)
                if not data:
                    break
                buffer.extend(data)
                while len(buffer) >= self.chunk_bytes:
                    chunk = bytes(buffer[: self.chunk_bytes])
                    del buffer[: self.chunk_bytes]
                    try:
                        self.chunks.put_nowait(chunk)
                    except queue.Full:
                        self.chunks.get_nowait()
                        self.chunks.put_nowait(chunk)

            self._ffmpeg.terminate()
            writer.join(timeout=2)
            self._ffmpeg = None
            self._response = None

    def _write_stream(self, response: requests.Response, process: subprocess.Popen) -> None:
        assert process.stdin is not None
        try:
            for block in response.iter_content(chunk_size=16384):
                if self.stop_event.is_set() or process.poll() is not None:
                    break
                if block:
                    process.stdin.write(block)
                    process.stdin.flush()
        except (OSError, requests.RequestException):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass


class BroadcastifyTakPipeline:
    def __init__(
        self,
        config: dict,
        sender,
        resolver: LocationResolver,
        status_callback: StatusCallback,
        stop_event: threading.Event,
    ):
        self.config = config
        self.sender = sender
        self.resolver = resolver
        self.status_callback = status_callback
        self.stop_event = stop_event
        self.log_transcripts = bool(config["service"].get("log_transcripts", False))
        self.min_confidence = float(config["whisper"]["min_confidence"])
        self.output = config["tak_output"]
        self.transcriber = WhisperTranscriber(config["whisper"])
        self.captures = [
            ChannelCapture(
                channel,
                config["broadcastify"],
                stop_event,
                status_callback,
            )
            for channel in config["broadcastify"]["channels"]
        ]
        self.workers: list[threading.Thread] = []

    def start(self) -> None:
        for capture in self.captures:
            capture.start()
            worker = threading.Thread(
                target=self._transcription_loop,
                args=(capture,),
                daemon=True,
                name=f"transcribe-{capture.source}",
            )
            worker.start()
            self.workers.append(worker)
        self.status_callback(state="running")

    def stop(self) -> None:
        self.stop_event.set()
        for capture in self.captures:
            capture.stop()
        for worker in self.workers:
            worker.join(timeout=5)

    def _transcription_loop(self, capture: ChannelCapture) -> None:
        while not self.stop_event.is_set():
            chunk = capture.get_chunk()
            if chunk is None:
                continue
            try:
                transcript, confidence = self.transcriber.transcribe(chunk)
                self._process(transcript, confidence, capture.label)
            except Exception as exc:
                log.exception("Transcription worker %s failed", capture.label)
                self.status_callback(error=str(exc))

    def _process(self, transcript: str, confidence: float, channel_label: str) -> None:
        if not transcript or confidence < self.min_confidence:
            return
        cleaned = clean_transcript(transcript, self.resolver.unit_map)
        if not cleaned or not should_send(cleaned):
            return

        location = self.resolver.resolve(cleaned)
        timestamp = datetime.now(timezone.utc).strftime("%H:%MZ")
        heading = f"[{channel_label} {timestamp}]"
        if location:
            heading += f" Location: {location.name} ({location.precision})"
        message = f"{heading}\n{cleaned}"

        chat = build_geochat(
            self.output["sender_uid"],
            self.output["sender_callsign"],
            self.output["chat_room"],
            message,
        )
        if not self.sender.send(chat):
            self.status_callback(error="TAK output queue is full")
            return

        if location and self.output.get("send_markers", True):
            incident, color = classify_incident(cleaned)
            callsign = f"{incident} {timestamp}"
            remarks = f"{cleaned[:350]}\n\nSource: Broadcastify transcript"
            marker = build_marker(
                f"broadcastify-{uuid.uuid4()}",
                "a-u-G",
                location.latitude,
                location.longitude,
                callsign,
                remarks,
                int(self.output.get("marker_stale_seconds", 1800)),
                color,
            )
            self.sender.send(marker)

        if self.log_transcripts:
            log.info("Sent transcript from %s: %s", channel_label, cleaned)
        else:
            log.info(
                "Sent transcript from %s (confidence %.2f, location=%s)",
                channel_label,
                confidence,
                bool(location),
            )
        self.status_callback(success=True)
