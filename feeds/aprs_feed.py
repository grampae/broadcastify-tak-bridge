#!/usr/bin/env python3
"""
aprs_feed.py
APRS-IS stream → CoT markers for mobile stations in AO
"""

import sys
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

from xml_utils import xml_attr, xml_text

log = logging.getLogger('aprs_feed')

try:
    import aprslib
except ImportError:
    log.error("aprslib not installed: pip install aprslib")
    sys.exit(1)

###############################################################################
# SYMBOL → COT TYPE
###############################################################################

# APRS symbol table/code → CoT type
SYMBOL_COT_MAP = {
    '>': 'a-f-G-U-C',   # car moving
    'k': 'a-f-G-U-C',   # truck
    'u': 'a-f-G-U-C',   # truck
    '^': 'a-f-A-C-F',   # aircraft
    '\'': 'a-f-A-C-F',  # aircraft
    'X': 'a-f-A-C-F',   # helicopter
    'R': 'a-f-G-E-V-A', # ambulance
    'f': 'a-f-G-E-V-F', # fire truck
    's': 'a-n-G',        # ship/boat
    'Y': 'a-n-S-X-M',   # yacht
    'O': 'b-m-p-s',     # balloon/emergency
    '/': 'a-f-G-U-C',   # generic waypoint
}

WEATHER_SYMBOLS = ['_', 'W']  # skip weather stations

def get_cot_type_from_symbol(symbol_table, symbol_code):
    if symbol_code in WEATHER_SYMBOLS:
        return None  # skip
    return SYMBOL_COT_MAP.get(symbol_code, 'a-f-G')

###############################################################################
# COT XML
###############################################################################

def cot_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_stale(seconds=1800):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds))\
           .strftime('%Y-%m-%dT%H:%M:%S.000Z')

def build_aprs_cot(callsign, cot_type, lat, lon, course, speed_kts,
                   altitude_m, comment, stale_seconds=1800):
    uid = f"aprs-{callsign.replace('-', '_')}"
    now = cot_now()
    stale = cot_stale(stale_seconds)
    speed_ms = speed_kts * 0.514444 if speed_kts else 0
    remarks = f"APRS: {callsign}"
    if comment:
        remarks += f" | {comment[:100]}"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid={xml_attr(uid)} type={xml_attr(cot_type)}
       how="m-g" time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}"
         hae="{altitude_m or 0:.1f}" ce="50" le="50"/>
  <detail>
    <contact callsign={xml_attr(callsign)}/>
    <track course="{course or 0}" speed="{speed_ms:.1f}"/>
    <remarks>{xml_text(remarks)}</remarks>
  </detail>
</event>'''

###############################################################################
# MAIN FEED CLASS
###############################################################################

class APRSFeed:
    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.aprs_config = config.get('feeds', {}).get('aprs', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.stale_seconds = config.get('stale_times', {}).get('aprs', 1800)
        self.callsign = self.aprs_config.get('callsign', 'NOCALL')
        self.passcode = str(self.aprs_config.get('passcode', -1))
        self.server = self.aprs_config.get('server', 'rotate.aprs2.net')
        self.port = self.aprs_config.get('port', 14580)
        self.filter_str = self.aprs_config.get('filter', 'r/46.7/-87.8/200')
        self.show_mobile = self.aprs_config.get('show_mobile', True)
        self.show_fixed = self.aprs_config.get('show_fixed', False)
        self.show_weather = self.aprs_config.get('show_weather', False)
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        self._ais = None

    def run(self):
        log.info(f"APRS feed starting (server: {self.server}, filter: {self.filter_str})")
        while True:
            try:
                self._connect()
            except Exception as e:
                self._handle_error(str(e))
            log.info("APRS reconnecting in 60s...")
            time.sleep(60)

    def _connect(self):
        self._ais = aprslib.IS(
            self.callsign,
            passwd=self.passcode,
            host=self.server,
            port=self.port
        )
        self._ais.set_filter(self.filter_str)
        log.info(f"APRS connected to {self.server}:{self.port}")
        self._ais.consumer(self._callback, raw=False)

    def _callback(self, packet):
        try:
            self._process(packet)
        except Exception as e:
            log.debug(f"APRS packet error: {e}")

    def _process(self, packet):
        callsign = packet.get('from', '')
        lat = packet.get('latitude')
        lon = packet.get('longitude')

        if not callsign or lat is None or lon is None:
            return

        # Symbol filtering
        symbol_table = packet.get('symbol_table', '/')
        symbol_code = packet.get('symbol', '/')

        if not self.show_weather and symbol_code in WEATHER_SYMBOLS:
            return

        # Speed = 0 and no recent movement = fixed station
        speed = packet.get('speed', 0)
        if not self.show_fixed and speed == 0 and symbol_code not in ['R', 'f', 'O']:
            return

        cot_type = get_cot_type_from_symbol(symbol_table, symbol_code)
        if cot_type is None:
            return

        uid = f"aprs-{callsign}"
        if self.dedup.seen(uid, ttl_seconds=60):
            return

        course = packet.get('course', 0)
        altitude = packet.get('altitude')  # meters
        comment = packet.get('comment', '')

        cot_xml = build_aprs_cot(
            callsign=callsign,
            cot_type=cot_type,
            lat=lat,
            lon=lon,
            course=course,
            speed_kts=speed,
            altitude_m=altitude,
            comment=comment,
            stale_seconds=self.stale_seconds
        )
        self.cot_sender.send(cot_xml)

        self.status.update({'ok': True,
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': self.status['last_count'] + 1})
        log.debug(f"APRS: {callsign} at {lat:.4f},{lon:.4f}")

    def _handle_error(self, msg):
        self.status['errors'] += 1
        self.status['last_error'] = msg
        log.error(f"APRS error: {msg}")
        if self._ais:
            try:
                self._ais.close()
            except:
                pass
