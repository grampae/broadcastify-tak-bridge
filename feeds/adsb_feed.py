#!/usr/bin/env python3
"""
adsb_feed.py
ADS-B aircraft positions → CoT markers

Sources:
  opensky:   OpenSky Network REST API (free, no feeder needed)
  local_sdr: dump1090/readsb JSON (base station via Netbird)

Enrichment:
  FAA database (local SQLite) → owner, make, model
  WEG database (local SQLite) → military type classification
"""

import re
import sys
import time
import json
import math
import logging
import sqlite3
import requests
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from xml_utils import xml_attr, xml_text

log = logging.getLogger('adsb_feed')

###############################################################################
# MILITARY HEX RANGES
###############################################################################

MILITARY_HEX_RANGES = [
    (0xAE0000, 0xAFFFFF),  # US Military
    (0x43C000, 0x43CFFF),  # UK Military
    (0x3B0000, 0x3BFFFF),  # France Military
]

def is_military_hex(hex_code):
    try:
        val = int(hex_code.replace('~', ''), 16)
        return any(lo <= val <= hi for lo, hi in MILITARY_HEX_RANGES)
    except:
        return False

###############################################################################
# AIRCRAFT TYPE → COT TYPE MAPPING
###############################################################################

# ICAO aircraft type designator prefixes → CoT type
AIRCRAFT_COT_MAP = {
    # Rotorcraft
    'H': 'a-f-A-C-H',    # civilian helicopter
    'G': 'a-f-A-C-H',    # autogyro
    # Fixed wing categories by weight/type handled below
}

UAV_TYPE_CODES = ['MQ1', 'MQ9', 'MQ4', 'RQ4', 'RQ7', 'RQ11', 'RQ170',
                  'PRED', 'REAP', 'GLBL', 'UAVX', 'UAV']

def get_cot_type(icao_hex, ac_type, is_mil, on_ground=False):
    """Determine CoT type from aircraft characteristics."""
    if is_mil:
        if ac_type and any(uav in ac_type.upper() for uav in UAV_TYPE_CODES):
            return 'a-f-A-M-F-Q-R'  # friendly military remotely piloted
        if ac_type and ac_type.upper().startswith('H'):
            return 'a-f-A-M-H-Q'    # friendly military helicopter
        return 'a-f-A-M-F-Q'        # friendly military fixed wing
    else:
        if ac_type and ac_type.upper().startswith('H'):
            return 'a-n-A-C-H'      # neutral civilian helicopter
        return 'a-n-A-C-F'          # neutral civilian fixed wing

###############################################################################
# FAA DATABASE MANAGER
###############################################################################

class FAADatabase:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self._ensure_db()

    def _ensure_db(self):
        if not self.db_path.exists():
            log.warning(f"FAA database not found at {self.db_path} — enrichment disabled")
            self._available = False
            return
        self._available = True
        log.info(f"FAA database loaded: {self.db_path}")

    def lookup(self, n_number):
        """Look up aircraft by N-number. Returns dict or None."""
        if not self._available:
            return None
        try:
            n = n_number.upper().lstrip('N').strip()
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                'SELECT owner, make, model, type_acft, type_eng FROM aircraft WHERE n_number=?',
                (n,)
            ).fetchone()
            conn.close()
            if row:
                return {'owner': row[0], 'make': row[1], 'model': row[2],
                        'type_acft': row[3], 'type_eng': row[4]}
        except Exception as e:
            log.debug(f"FAA lookup failed for {n_number}: {e}")
        return None

class WEGDatabase:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self._available = self.db_path.exists()
        if self._available:
            log.info(f"WEG database loaded: {self.db_path}")

    def lookup(self, type_code):
        """Look up aircraft type. Returns dict or None."""
        if not self._available or not type_code:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                'SELECT name, category, cot_type, military FROM aircraft WHERE type_code=?',
                (type_code.upper(),)
            ).fetchone()
            conn.close()
            if row:
                return {'name': row[0], 'category': row[1],
                        'cot_type': row[2], 'military': bool(row[3])}
        except Exception as e:
            log.debug(f"WEG lookup failed for {type_code}: {e}")
        return None

###############################################################################
# COT XML BUILDER
###############################################################################

def cot_time(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

def build_aircraft_cot(uid, cot_type, lat, lon, alt_ft, speed_kts,
                       heading, callsign, remarks, stale_seconds=600):
    now = cot_time()
    stale = cot_time(datetime.now(timezone.utc) + timedelta(seconds=stale_seconds))
    alt_m = alt_ft * 0.3048 if alt_ft else 0
    speed_ms = speed_kts * 0.514444 if speed_kts else 0
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid={xml_attr(uid)} type={xml_attr(cot_type)}
       how="m-g" time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}" hae="{alt_m:.1f}" ce="50" le="50"/>
  <detail>
    <contact callsign={xml_attr(callsign)}/>
    <track course="{heading or 0}" speed="{speed_ms:.1f}"/>
    <remarks>{xml_text(remarks)}</remarks>
  </detail>
</event>'''

###############################################################################
# OPENSKY SOURCE
###############################################################################

class OpenSkySource:
    URL = 'https://opensky-network.org/api/states/all'

    def __init__(self, config):
        self.bbox = config['area_of_interest']
        self.username = config.get('opensky_username', '')
        self.password = config.get('opensky_password', '')
        self._last_response = None

    def fetch(self):
        params = {
            'lamin': self.bbox['south'],
            'lomin': self.bbox['west'],
            'lamax': self.bbox['north'],
            'lomax': self.bbox['east'],
        }
        auth = (self.username, self.password) if self.username else None
        resp = requests.get(self.URL, params=params, auth=auth, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._last_response = data
        return self._parse(data)

    def _parse(self, data):
        aircraft = []
        for state in (data.get('states') or []):
            if state[5] is None or state[6] is None:
                continue
            aircraft.append({
                'hex':       (state[0] or '').strip(),
                'callsign':  (state[1] or '').strip(),
                'lon':       state[5],
                'lat':       state[6],
                'alt_ft':    state[7] * 3.28084 if state[7] else None,
                'on_ground': state[8],
                'speed_kts': state[9] * 1.94384 if state[9] else None,
                'heading':   state[10],
                'squawk':    state[14],
                'type':      None,  # OpenSky doesn't return type
                'reg':       None,
            })
        return aircraft

###############################################################################
# LOCAL SDR SOURCE (dump1090/readsb)
###############################################################################

class LocalSDRSource:
    def __init__(self, config):
        self.url = config.get('local_sdr_url',
                   'http://localhost:8080/data/aircraft.json')
        self.bbox = config['area_of_interest']

    def fetch(self):
        resp = requests.get(self.url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return self._parse(data)

    def _parse(self, data):
        aircraft = []
        for ac in data.get('aircraft', []):
            lat = ac.get('lat')
            lon = ac.get('lon')
            if lat is None or lon is None:
                continue
            # Bbox filter
            b = self.bbox
            if not (b['south'] <= lat <= b['north'] and
                    b['west'] <= lon <= b['east']):
                continue
            # Skip stale (seen > 60s ago)
            if ac.get('seen', 0) > 60:
                continue
            aircraft.append({
                'hex':       ac.get('hex', '').strip(),
                'callsign':  ac.get('flight', '').strip(),
                'lon':       lon,
                'lat':       lat,
                'alt_ft':    ac.get('altitude') or ac.get('alt_baro'),
                'on_ground': ac.get('altitude') == 0,
                'speed_kts': ac.get('speed'),
                'heading':   ac.get('track'),
                'squawk':    ac.get('squawk'),
                'type':      ac.get('category'),
                'reg':       None,
            })
        return aircraft

###############################################################################
# MAIN FEED CLASS
###############################################################################

class ADSBFeed:
    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.ac_config = config.get('feeds', {}).get('adsb', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.stale_seconds = config.get('stale_times', {}).get('adsb', 600)
        self.poll_interval = self.ac_config.get('poll_interval', 30)
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}

        source = self.ac_config.get('source', 'opensky')
        if source == 'local_sdr':
            self.source = LocalSDRSource(self.ac_config)
        else:
            self.source = OpenSkySource({**self.ac_config,
                                         'area_of_interest': config['area_of_interest']})

        faa_path = self.ac_config.get('faa_db_path',
                   str(Path.home() / 'manpack-feeds/data/faa_aircraft.db'))
        weg_path = self.ac_config.get('weg_db_path',
                   str(Path.home() / 'manpack-feeds/data/weg_aircraft.db'))
        self.faa = FAADatabase(faa_path)
        self.weg = WEGDatabase(weg_path)

        self._backoff = 1
        self._last_cached = []

    def run(self):
        log.info(f"ADS-B feed starting (source: {self.ac_config.get('source', 'opensky')}, "
                 f"interval: {self.poll_interval}s)")
        while True:
            try:
                aircraft_list = self.source.fetch()
                self._backoff = 1
                count = 0
                for ac in aircraft_list:
                    if self._process(ac):
                        count += 1
                self.status.update({'ok': True,
                    'last_success': datetime.now(timezone.utc).isoformat(),
                    'last_count': count})
                log.debug(f"ADS-B: {count} aircraft pushed")
            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    wait = min(self._backoff * 60, 1800)
                    log.warning(f"ADS-B rate limited — waiting {wait}s")
                    time.sleep(wait)
                    self._backoff *= 2
                    continue
                else:
                    self._handle_error(str(e))
            except Exception as e:
                self._handle_error(str(e))

            time.sleep(self.poll_interval)

    def _process(self, ac):
        hex_code = ac.get('hex', '')
        if not hex_code:
            return False

        # Skip ground vehicles
        if ac.get('on_ground') and not hex_code:
            return False

        uid = f"adsb-{hex_code}"

        # Dedup — aircraft positions update frequently, use short TTL
        if self.dedup.seen(uid, ttl_seconds=25):
            return False

        is_mil = is_military_hex(hex_code) or bool(
            ac.get('squawk') in ['7700', '7600', '7500']
        )

        ac_type = ac.get('type', '')
        weg_info = self.weg.lookup(ac_type) if ac_type else None
        if weg_info and weg_info.get('military'):
            is_mil = True

        # Skip if config says not to show
        if is_mil and not self.ac_config.get('show_military', True):
            return False
        if not is_mil and not self.ac_config.get('show_commercial', True):
            return False

        # Altitude filter
        alt = ac.get('alt_ft')
        max_alt = self.ac_config.get('altitude_max_ft', 50000)
        if alt and alt > max_alt:
            return False

        cot_type = get_cot_type(hex_code, ac_type, is_mil, ac.get('on_ground'))
        if weg_info and weg_info.get('cot_type'):
            cot_type = weg_info['cot_type']

        callsign = ac.get('callsign') or hex_code.upper()

        # Build remarks with enrichment
        remarks_parts = []
        if is_mil:
            remarks_parts.append('MILITARY')
        if weg_info:
            remarks_parts.append(f"Type: {weg_info['name']}")
        faa_info = self.faa.lookup(ac.get('reg', '')) if ac.get('reg') else None
        if faa_info:
            remarks_parts.append(f"Owner: {faa_info['owner']}")
            remarks_parts.append(f"Aircraft: {faa_info['make']} {faa_info['model']}")
        if ac_type:
            remarks_parts.append(f"ICAO Type: {ac_type}")
        if ac.get('squawk'):
            remarks_parts.append(f"Squawk: {ac['squawk']}")
        remarks_parts.append(f"Hex: {hex_code}")
        remarks = ' | '.join(remarks_parts)

        cot_xml = build_aircraft_cot(
            uid=uid,
            cot_type=cot_type,
            lat=ac['lat'],
            lon=ac['lon'],
            alt_ft=alt,
            speed_kts=ac.get('speed_kts'),
            heading=ac.get('heading'),
            callsign=callsign,
            remarks=remarks,
            stale_seconds=self.stale_seconds
        )
        self.cot_sender.send(cot_xml)
        return True

    def _handle_error(self, msg):
        self.status['errors'] += 1
        self.status['last_error'] = msg
        log.error(f"ADS-B error: {msg}")
