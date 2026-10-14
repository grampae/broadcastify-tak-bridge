#!/usr/bin/env python3
"""
broadcastify_feed.py
Broadcastify scanner -> ATAK GeoChat + CoT markers

Stream: stereo MP3
Left:  Police/Fire/EMS dispatch
Right: Paging/Fire/EMS/Air

STT: faster-whisper (CPU, int8 quantized)
Locations: loaded from locations.yml (no hardcoded data)
"""

import re
import sys
import json
import time
import uuid
import queue
import logging
import argparse
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from xml_utils import xml_attr, xml_text

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] broadcastify: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('broadcastify')

###############################################################################
# LOCATIONS — loaded from locations.yml
###############################################################################

def load_locations(locations_file='/app/locations.yml'):
    """Load towns, landmarks, roads and unit_map from YAML file."""
    import yaml
    defaults = {'towns': {}, 'landmarks': {}, 'roads': {}, 'unit_map': {}}
    path = Path(locations_file)
    if not path.exists():
        log.warning(f"locations.yml not found at {path} — location resolution disabled")
        return defaults
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        result = {'towns': {}, 'landmarks': {}, 'roads': {}, 'unit_map': {}}
        for section in ['towns', 'landmarks', 'roads']:
            for name, values in (data.get(section) or {}).items():
                lat, lon = values[0], values[1]
                aliases = values[2] if len(values) > 2 else []
                result[section][name] = (lat, lon, aliases)
        result['unit_map'] = data.get('unit_map') or {}
        log.info(f"Loaded locations: {len(result['towns'])} towns, "
                 f"{len(result['landmarks'])} landmarks, "
                 f"{len(result['roads'])} roads, "
                 f"{len(result['unit_map'])} unit mappings")
        return result
    except Exception as e:
        log.error(f"Failed to load locations.yml: {e}")
        return defaults

###############################################################################
# INCIDENT PATTERNS
###############################################################################

INCIDENT_PATTERNS = {
    'fire': {
        'keywords': ['fire', 'structure fire', 'building fire', 'vehicle fire',
                     'brush fire', 'wildfire', 'smoke', '9501', 'engine', 'ladder',
                     'fb', 'fc', 'fr', 'fs'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'FIRE', 'color': '-23296',
    },
    'medical': {
        'keywords': ['medical', 'ems', 'ambulance', 'cardiac', 'breathing',
                     '10-52', 'life flight', 'unresponsive', 'overdose', '9809',
                     'bls', 'als', 'medx'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'EMS', 'color': '-16711936',
    },
    'police': {
        'keywords': ['10-99', 'shots fired', 'signal 4', '10-13', 'pursuit',
                     'armed', 'assault', 'backup', 'trooper needs', 'shots',
                     'weapon', '10-32', '10-89', '10-80'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'POLICE', 'color': '-16776961',
    },
    'accident': {
        'keywords': ['10-50', '10-49', 'accident', 'collision', 'crash', 'mva',
                     'mvc', 'mvce', 'mvcf'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'ACCIDENT', 'color': '-256',
    },
    'hazmat': {
        'keywords': ['hazmat', 'haz-mat', '9506', 'chemical', 'spill', 'gas leak', 'hz'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'HAZMAT', 'color': '-16711936',
    },
    'search': {
        'keywords': ['missing', 'search and rescue', 'sar', 'overdue', 'lost person'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'SAR', 'color': '-256',
    },
    'emergency': {
        'keywords': ['10-33', 'mayday', 'officer down', 'trooper needs emergency'],
        'cot_type': 'a-u-G', 'callsign_prefix': 'EMERGENCY', 'color': '-65536',
    },
}

ADDRESS_PATTERNS = [
    r'\b(\d+)\s+(north|south|east|west|n\.?|s\.?|e\.?|w\.?)?\s*([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\s+(street|avenue|road|drive|way|boulevard|lane|court|place|ave|st|rd|dr|blvd|ln|ct|pl)\b',
    r'\b(us[-\s]?\d+|m[-\s]?\d+|cr[-\s]?\d+|county\s+road\s+\d+)\b',
]

SKIP_PATTERNS = [
    r'^\s*10[-\s]?4\.?\s*$',
    r'^\s*copy\.?\s*$',
    r'^\s*clear\.?\s*$',
    r'^\s*(dispatch|county),?\s*(go ahead|copy|clear|10-4)\.?\s*$',
    r'^\s*\d+[-\s]\d+\s*$',
    r'^\s*(radio check|signal check)\.?\s*$',
    r'^\s*(affirmative|negative|roger|wilco)\.?\s*$',
    r'^\s*thank you\.?\s*$',
    r"^\s*you're welcome\.?\s*$",
    r'^\s*bye[-\s]?bye\.?\s*$',
    r'^\s*go ahead\.?\s*$',
]

HALLUCINATION_BLOCKS = [
    r'^\s*\[.*?\]\s*$',
    r'^\s*\(.*?\)\s*$',
    r'(?i)^\s*sound(s)? (of|effect)',
    r'(?i)^\s*transcript(ion)? (by|from)',
    r'(?i)^\s*subtitles? (by|from)',
    r'(?i)^\s*www\.',
    r'(?i)thank you for watching',
    r'(?i)please (like|subscribe)',
    r'(?i)^\s*\.+\s*$',
    r'(?i)^\s*[!\*]+\s*$',
    r'(?i)^\s*(uh+|um+|mm+|hmm+)\s*\.?\s*$',
]

CHANNEL_LABELS = {'left': 'DISPATCH-L', 'right': 'DISPATCH-R'}
CHANNEL_DESC = {'left': 'Police/Fire/EMS', 'right': 'Paging/Fire/EMS/Air'}

###############################################################################
# PUSHOVER ALERTS
###############################################################################

def send_pushover(message, user_key, app_token):
    try:
        import requests as req
        req.post('https://api.pushover.net/1/messages.json', data={
            'token': app_token,
            'user': user_key,
            'message': message
        }, timeout=5)
    except Exception as e:
        log.error(f"Pushover error: {e}")

def check_pushover_alert(transcript, location_name, bc_config):
    user_key = bc_config.get('pushover_user_key')
    app_token = bc_config.get('pushover_app_token')
    keywords = bc_config.get('alert_keywords', [])
    if not user_key or not app_token or not keywords:
        return
    t = transcript.lower()
    matched = [kw for kw in keywords if kw.lower() in t]
    if not matched:
        return
    msg = transcript
    if location_name:
        msg = 'Location: ' + location_name + '\n\n' + transcript
    send_pushover(msg, user_key, app_token)
    log.info(f"Pushover alert sent: {matched}")

###############################################################################
# LOCATION RESOLVER
###############################################################################

class LocationResolver:
    def __init__(self, locations_file='/app/locations.yml', address_db=None,
                 geocode_suffix='Michigan, USA'):
        locs = load_locations(locations_file)
        self.lookup = {}
        all_locs = {}
        all_locs.update(locs.get('landmarks', {}))
        all_locs.update(locs.get('towns', {}))
        all_locs.update(locs.get('roads', {}))
        for name, data in all_locs.items():
            lat, lon = data[0], data[1]
            aliases = data[2] if len(data) > 2 else []
            self.lookup[name] = (lat, lon, name)
            for alias in aliases:
                self.lookup[alias.lower()] = (lat, lon, name)
        self.unit_map = locs.get('unit_map', {})
        self.address_db = address_db
        self.geocode_suffix = geocode_suffix

    def resolve(self, transcript):
        t = transcript.lower()
        for pattern in ADDRESS_PATTERNS:
            match = re.search(pattern, transcript, re.IGNORECASE)
            if match:
                addr_str = match.group(0)
                result = self._geocode(addr_str)
                if result:
                    return result[0], result[1], addr_str, 'EXACT'
        best = None
        best_len = 0
        for keyword, data in self.lookup.items():
            if keyword in t and len(keyword) > best_len:
                best = data
                best_len = len(keyword)
        if best:
            return best[0], best[1], best[2], 'APPROX'
        return None, None, None, 'NO LOCATION'

    def _geocode(self, address):
        # Try local address database first
        try:
            import sqlite3
            db_path = self.address_db or '/app/data/marquette_addresses.db'
            if Path(db_path).exists():
                sys.path.insert(0, '/app')
                from build_address_db import lookup_address
                lat, lon, matched = lookup_address(db_path, address)
                if lat is not None:
                    return lat, lon
        except Exception as e:
            log.debug(f"Local geocode error: {e}")
        # Nominatim fallback
        try:
            import requests
            resp = requests.get(
                'https://nominatim.openstreetmap.org/search',
                params={'q': f'{address}, {self.geocode_suffix}', 'format': 'json',
                        'limit': 1, 'countrycodes': 'us'},
                headers={'User-Agent': 'ManpackFeeds/1.0'}, timeout=5
            )
            data = resp.json()
            if data:
                return float(data[0]['lat']), float(data[0]['lon'])
        except Exception as e:
            log.debug(f"Nominatim error: {e}")
        return None

###############################################################################
# TRANSCRIPT PROCESSING
###############################################################################

def detect_repetition_cascade(text):
    words = text.split()
    if len(words) < 6:
        return False
    for n in range(2, 6):
        for i in range(len(words) - n * 2):
            ngram = ' '.join(words[i:i+n])
            if text.count(ngram) >= 3:
                return True
    return False

def filter_hallucinations(text):
    if not text:
        return ''
    t = text.strip()
    for pattern in HALLUCINATION_BLOCKS:
        if re.match(pattern, t):
            return ''
    if detect_repetition_cascade(t):
        log.debug(f"Repetition cascade: {t[:60]!r}")
        return ''
    t = re.sub(r'(?i)\b(beep+|boop+|ding|dong|alert tone|tone out)\b', '[beeps]', t)
    t = re.sub(r'\s*\.\.\.$', '', t).strip()
    t = re.sub(r'\s*-+\s*$', '', t).strip()
    t = re.sub(r'\s*,\s*$', '', t).strip()
    return t

def should_send(transcript):
    t = transcript.strip()
    if len(t.split()) < 5:
        return False
    tl = t.lower()
    for pattern in SKIP_PATTERNS:
        if re.match(pattern, tl, re.IGNORECASE):
            return False
    return True

def apply_unit_map(text, unit_map):
    t = text
    for spoken, canonical in unit_map.items():
        t = re.sub(r'(?i)\b' + re.escape(str(spoken)) + r'\b', str(canonical), t)
    return t

def clean_transcript(text, unit_map=None):
    text = filter_hallucinations(text)
    if not text:
        return ''
    if unit_map:
        text = apply_unit_map(text, unit_map)
    return text.strip()

def classify_incident(transcript):
    t = transcript.lower()
    for incident_type, config in INCIDENT_PATTERNS.items():
        for keyword in config['keywords']:
            if keyword in t:
                return (incident_type, config['cot_type'],
                        config['callsign_prefix'], config.get('color', '-256'))
    return 'general', 'a-u-G', 'DISPATCH', '-256'

###############################################################################
# COT XML
###############################################################################

def cot_time(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_stale(seconds):
    return cot_time(datetime.now(timezone.utc) + timedelta(seconds=seconds))

def build_geochat(sender_uid, sender_callsign, room_name, message):
    now = cot_time()
    stale = cot_stale(3600)
    msg_uid = str(uuid.uuid4())
    event_uid = 'GeoChat.' + sender_uid + '.' + room_name + '.' + msg_uid
    safe_msg = xml_text(message)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<event version="2.0" uid=' + xml_attr(event_uid) + ' type="b-t-f" '
        'how="h-e" time="' + now + '" start="' + now + '" stale="' + stale + '">\n'
        '  <point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>\n'
        '  <detail>\n'
        '    <__chat id=' + xml_attr(room_name) + ' chatroom=' + xml_attr(room_name) + ' '
        'groupOwner="false" messageId=' + xml_attr(msg_uid) + ' senderCallsign=' + xml_attr(sender_callsign) + '>\n'
        '      <chatgrp uid0=' + xml_attr(sender_uid) + ' uid1=' + xml_attr(room_name) + ' id=' + xml_attr(room_name) + '/>\n'
        '    </__chat>\n'
        '    <link uid=' + xml_attr(sender_uid) + ' type="a-f-G-U-C" relation="p-p"/>\n'
        '    <remarks source=' + xml_attr('BAO.F.ATAK.' + sender_uid) + ' time="' + now + '">' + safe_msg + '</remarks>\n'
        '  </detail>\n'
        '</event>'
    )

def build_cot_marker(uid, cot_type, lat, lon, callsign, remarks,
                     stale_seconds=1800, color='-256'):
    now = cot_time()
    stale = cot_stale(stale_seconds)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<event version="2.0" uid=' + xml_attr(uid) + ' type=' + xml_attr(cot_type) + ' '
        'how="h-g-i-g-o" time="' + now + '" start="' + now + '" stale="' + stale + '">\n'
        '  <point lat="' + str(lat) + '" lon="' + str(lon) + '" hae="0" ce="999999" le="999999"/>\n'
        '  <detail>\n'
        '    <contact callsign=' + xml_attr(callsign) + '/>\n'
        '    <color argb=' + xml_attr(color) + '/>\n'
        '    <remarks>' + xml_text(remarks[:400]) + '</remarks>\n'
        '  </detail>\n'
        '</event>'
    )

###############################################################################
# FASTER-WHISPER STT
###############################################################################

class WhisperSTT:
    def __init__(self, model_size='medium.en', device='cpu', compute_type='int8'):
        try:
            from faster_whisper import WhisperModel
            log.info(f"Loading faster-whisper model: {model_size} ({compute_type})")
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            log.info("faster-whisper model loaded")
        except ImportError:
            log.error("faster-whisper not installed")
            raise
        except Exception as e:
            log.error(f"faster-whisper load failed: {e}")
            raise

    def transcribe(self, pcm_bytes, initial_prompt=None):
        try:
            import numpy as np
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            kwargs = dict(
                language='en',
                beam_size=12,
                patience=2.0,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=300,
                    speech_pad_ms=400,
                    threshold=0.3,
                ),
                condition_on_previous_text=False,
            )
            if initial_prompt:
                kwargs['initial_prompt'] = initial_prompt
            segments, info = self.model.transcribe(audio, **kwargs)
            texts = []
            probs = []
            for seg in segments:
                if seg.text.strip():
                    texts.append(seg.text.strip())
                    probs.append(seg.avg_logprob)
            if not texts:
                return '', 0.0
            full_text = ' '.join(texts)
            avg_conf = min(1.0, max(0.0, (sum(probs) / len(probs) + 1.0)))
            return full_text, avg_conf
        except Exception as e:
            log.error(f"Whisper error: {e}")
            return '', 0.0

###############################################################################
# CHANNEL CAPTURE
###############################################################################

class ChannelCapture:
    CHUNK_SIZE = 16000 * 2 * 10  # 10 seconds of 16kHz mono 16-bit

    def __init__(self, stream_url, username, password, channel='left'):
        self.stream_url = stream_url
        self.username = username
        self.password = password
        self.channel = channel
        self.pan_filter = 'pan=mono|c0=c0' if channel == 'left' else 'pan=mono|c0=c1'
        self._curl = None
        self._ffmpeg = None
        self._chunk_queue = queue.Queue(maxsize=100)
        self._running = False

    def start(self):
        self._running = True
        t = threading.Thread(target=self._capture_loop, daemon=True,
                             name='capture-' + self.channel)
        t.start()
        return t

    def _capture_loop(self):
        retry_delay = 5
        while self._running:
            try:
                log.info(f"Connecting to Broadcastify ({self.channel})...")
                self._curl = subprocess.Popen([
                    'curl', '-s', '-u',
                    self.username + ':' + self.password,
                    self.stream_url
                ], stdout=subprocess.PIPE)
                self._ffmpeg = subprocess.Popen([
                    'ffmpeg', '-loglevel', 'error',
                    '-i', 'pipe:0',
                    '-af', self.pan_filter,
                    '-ar', '16000',
                    '-f', 's16le', 'pipe:1'
                ], stdin=self._curl.stdout,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._curl.stdout.close()
                log.info(f"Stream connected ({self.channel}: {CHANNEL_DESC[self.channel]})")
                retry_delay = 5
                buffer = b''
                while self._running:
                    data = self._ffmpeg.stdout.read(4096)
                    if not data:
                        log.warning(f"Stream ended ({self.channel})")
                        break
                    buffer += data
                    while len(buffer) >= self.CHUNK_SIZE:
                        chunk = buffer[:self.CHUNK_SIZE]
                        buffer = buffer[self.CHUNK_SIZE:]
                        try:
                            self._chunk_queue.put_nowait(chunk)
                        except queue.Full:
                            try:
                                self._chunk_queue.get_nowait()
                                self._chunk_queue.put_nowait(chunk)
                            except:
                                pass
            except Exception as e:
                log.error(f"Capture error ({self.channel}): {e}")
            finally:
                for proc in [self._ffmpeg, self._curl]:
                    if proc:
                        try: proc.terminate()
                        except: pass
                self._curl = None
                self._ffmpeg = None
            if self._running:
                log.info(f"Reconnecting ({self.channel}) in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)

    def get_chunk(self, timeout=15):
        try:
            return self._chunk_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self._running = False
        for proc in [getattr(self, '_ffmpeg', None), getattr(self, '_curl', None)]:
            if proc:
                try: proc.terminate()
                except: pass

###############################################################################
# MAIN FEED CLASS
###############################################################################

class BroadcastifyFeed:
    def __init__(self, config, cot_sender, dedup=None, whisper=None):
        self.config = config
        self.bc_config = config.get('feeds', {}).get('broadcastify', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        self.sender_uid = self.bc_config.get('sender_uid', 'dispatch-feed')
        self.sender_callsign = self.bc_config.get('sender_callsign', 'DISPATCH')
        self.chat_room = self.bc_config.get('chat_room', 'All Chat Rooms')
        self.min_confidence = self.bc_config.get('min_confidence', 0.3)
        locations_file = self.bc_config.get('locations_file', '/app/locations.yml')
        self.resolver = LocationResolver(
            locations_file=locations_file,
            address_db=self.bc_config.get('address_db'),
            geocode_suffix=self.bc_config.get('geocode_suffix', 'Michigan, USA'),
        )
        self.unit_map = self.resolver.unit_map
        self._whisper = whisper  # shared model if provided
        self.initial_prompt = self.bc_config.get('initial_prompt',
            'Marquette County dispatch. Engine, Ladder, Medic, MSP, DNR. '
            'Negaunee, Ishpeming, Gwinn, Big Bay. US-41, M-28, County Road. '
            '10-4, 10-50, 10-52, structure fire, MVC, EMS.')

    def run(self):
        stream_url = self.bc_config.get('stream_url')
        username = self.bc_config.get('premium_username', '')
        password = self.bc_config.get('premium_password', '')
        if not stream_url:
            log.error("No stream_url configured")
            return

        # Use shared whisper model or load our own
        if self._whisper:
            stt = self._whisper
            log.info("Broadcastify using shared Whisper model")
        else:
            model_size = self.bc_config.get('whisper_model', 'medium.en')
            stt = WhisperSTT(model_size=model_size, device='cpu', compute_type='int8')

        transcript_queue = queue.Queue(maxsize=200)

        def stt_worker(cap, ch_name):
            log.info(f"STT worker started for {ch_name} channel")
            while True:
                chunk = cap.get_chunk(timeout=15)
                if chunk is None:
                    transcript_queue.put((ch_name, '', 0.0))
                    continue
                text, conf = stt.transcribe(chunk, initial_prompt=self.initial_prompt)
                if text:
                    log.info(f"[{ch_name}] transcribed: {text[:80]!r} (conf={conf:.2f})")
                transcript_queue.put((ch_name, text, conf))

        left_cap = ChannelCapture(stream_url, username, password, 'left')
        right_cap = ChannelCapture(stream_url, username, password, 'right')
        left_cap.start()
        right_cap.start()

        threading.Thread(target=stt_worker, args=(left_cap, 'left'),
                         daemon=True, name='stt-left').start()
        threading.Thread(target=stt_worker, args=(right_cap, 'right'),
                         daemon=True, name='stt-right').start()

        log.info("Broadcastify feed running")

        while True:
            try:
                ch_name, text, conf = transcript_queue.get(timeout=30)
            except queue.Empty:
                continue

            if not text:
                continue

            if conf < self.min_confidence:
                log.info(
                    f"Low-confidence transcript filtered: {conf:.2f} "
                    f"< {self.min_confidence:.2f}"
                )
                continue

            cleaned = clean_transcript(text, unit_map=self.unit_map)
            if not cleaned:
                log.debug(f"Hallucination: {text[:60]!r}")
                continue

            if not should_send(cleaned):
                log.info(f"Filtered: {cleaned[:80]!r}")
                continue

            self._send(cleaned, ch_name, conf)

    def _send(self, transcript, channel, conf):
        timestamp = datetime.now().strftime('%H:%M')
        incident_type, cot_type, callsign_prefix, color = classify_incident(transcript)
        lat, lon, location_name, tier = self.resolver.resolve(transcript)

        label = CHANNEL_LABELS.get(channel, 'DISPATCH')

        if tier == 'EXACT':
            chat_msg = '[' + label + ' ' + timestamp + '] Loc: ' + location_name + '\n' + transcript
        else:
            chat_msg = '[' + label + ' ' + timestamp + ']\n' + transcript

        geochat_xml = build_geochat(self.sender_uid, self.sender_callsign,
                                    self.chat_room, chat_msg)
        self.cot_sender.send(geochat_xml)
        log.info(f"GeoChat sent [{tier}]: {transcript[:80]}...")
        check_pushover_alert(transcript, location_name, self.bc_config)

        if lat is not None and lon is not None:
            if tier == 'EXACT':
                callsign = callsign_prefix + ' ' + timestamp
            else:
                callsign = '[APPROX] ' + callsign_prefix + ' ' + timestamp
            remarks = transcript[:400] + '\n\nSource: Dispatch'
            if location_name:
                remarks += '\nArea: ' + location_name
            marker_uid = 'broadcastify-' + incident_type + '-' + str(int(time.time()))
            cot_xml = build_cot_marker(marker_uid, cot_type, lat, lon,
                                       callsign, remarks,
                                       self.bc_config.get('stale_seconds', 1800),
                                       color=color)
            self.cot_sender.send(cot_xml)
            log.info(f"CoT marker: {callsign_prefix} at {location_name} ({tier})")

        self.status['ok'] = True
        self.status['last_success'] = datetime.now(timezone.utc).isoformat()
        self.status['last_count'] += 1

###############################################################################
# ENTRY POINT
###############################################################################

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='feeds.yml')
    args = parser.parse_args()
    import yaml
    with open(args.config) as f:
        config = yaml.safe_load(f)
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from cot_sender import COTSender
    sender = COTSender(config)
    feed = BroadcastifyFeed(config, sender)
    try:
        feed.run()
    except KeyboardInterrupt:
        log.info("Stopped")
