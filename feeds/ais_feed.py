#!/usr/bin/env python3
"""
ais_feed.py
AIS vessel positions -> CoT markers via AISstream.io WebSocket
Uses synchronous websocket-client to avoid asyncio/thread conflicts.
"""

import json
import time
import logging
import websocket
from datetime import datetime, timezone, timedelta

from xml_utils import xml_attr, xml_text

log = logging.getLogger('ais_feed')
#log.setLevel(logging.DEBUG)

def get_vessel_cot_type(ship_type):
    if ship_type is None:
        return 'a-u-S-X-M'
    st = int(ship_type)
    if 30 <= st <= 39:   return 'a-n-S-X-M-F'
    if 60 <= st <= 69:   return 'a-n-S-X-M-P'
    if 70 <= st <= 79:   return 'a-n-S-X-M-C'
    if 80 <= st <= 89:   return 'a-n-S-X-M-T'
    if st in [35, 36]:   return 'a-f-S-X-M'
    return 'a-n-S-X-M'

NAVIGATIONAL_STATUS = {
    0: 'Under way (engine)', 1: 'At anchor', 2: 'Not under command',
    3: 'Restricted maneuverability', 5: 'Moored', 6: 'Aground',
    8: 'Under way (sailing)', 15: 'Unknown',
}

def cot_time(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_stale(seconds=1800):
    return cot_time(datetime.now(timezone.utc) + timedelta(seconds=seconds))

def build_vessel_cot(mmsi, cot_type, lat, lon, sog, cog, ship_name, nav_status, stale_seconds=1800):
    uid = 'ais-' + str(mmsi)
    now = cot_time()
    stale = cot_stale(stale_seconds)
    speed_ms = round(sog * 0.514444, 1) if sog else 0
    callsign = ship_name.strip() if ship_name and ship_name.strip() else 'MMSI:' + str(mmsi)
    status_str = NAVIGATIONAL_STATUS.get(nav_status, 'Unknown')
    remarks = 'MMSI: ' + str(mmsi) + ' | ' + status_str
    if sog:
        remarks += ' | ' + str(round(sog, 1)) + 'kts'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<event version="2.0" uid=' + xml_attr(uid) + ' type=' + xml_attr(cot_type) + ' '
        'how="m-g" time="' + now + '" start="' + now + '" stale="' + stale + '">\n'
        '  <point lat="' + str(lat) + '" lon="' + str(lon) + '" hae="0" ce="100" le="100"/>\n'
        '  <detail>\n'
        '    <contact callsign=' + xml_attr(callsign) + '/>\n'
        '    <track course="' + str(cog or 0) + '" speed="' + str(speed_ms) + '"/>\n'
        '    <remarks>' + xml_text(remarks) + '</remarks>\n'
        '  </detail>\n'
        '</event>'
    )

class AISFeed:
    WS_URL = 'wss://stream.aisstream.io/v0/stream'

    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.ais_config = config.get('feeds', {}).get('vessel', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.stale_seconds = config.get('stale_times', {}).get('vessel', 1800)
        self.api_key = self.ais_config.get('api_key', '')
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        ext = config.get('extended_area', config['area_of_interest'])
        self.bbox = [[ext['south'], ext['west']], [ext['north'], ext['east']]]

    def run(self):
        log.info('AIS feed starting... bbox: ' + str(self.bbox))
        while True:
            try:
                self._connect()
            except Exception as e:
                self.status['errors'] += 1
                self.status['last_error'] = str(e)
                log.error('AIS error: ' + str(e))
            log.info('AIS reconnecting in 30s...')
            time.sleep(30)

    def _connect(self):
        subscribe_msg = json.dumps({
            'APIKey': self.api_key,
            'BoundingBoxes': [self.bbox],
            'FilterMessageTypes': [
                'PositionReport',
                'StandardClassBPositionReport',
                'ExtendedClassBPositionReport'
            ]
        })

        ws = websocket.create_connection(self.WS_URL, timeout=60)
        ws.send(subscribe_msg)
        log.info('AIS WebSocket connected, bbox: ' + str(self.bbox))

        while True:
            try:
                raw = ws.recv()
                if not raw:
                    log.warning('AIS WebSocket closed')
                    break
                msg = json.loads(raw)
                self._process(msg)
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                log.warning('AIS connection closed')
                break
            except Exception as e:
                log.debug('AIS recv error: ' + str(e))
                continue

        ws.close()

    def _process(self, msg):
        meta = msg.get('MetaData', {})
        mmsi = meta.get('MMSI')
        lat = meta.get('latitude') or meta.get('Latitude')
        lon = meta.get('longitude') or meta.get('Longitude')
        if not mmsi or lat is None or lon is None:
            log.debug('AIS: missing mmsi/lat/lon')
            return
        if abs(lat) > 90 or abs(lon) > 180:
            log.debug('AIS: invalid coords ' + str(lat) + ',' + str(lon))
            return
        if lat == 0 and lon == 0:
            log.debug('AIS: zero coords')
            return

        uid = 'ais-' + str(mmsi)
        if self.dedup.seen(uid, ttl_seconds=60):
            return

        ship_name = meta.get('ShipName', '').strip()
        msg_data = msg.get('Message', {})
        pos = (msg_data.get('PositionReport') or
               msg_data.get('StandardClassBPositionReport') or
               msg_data.get('ExtendedClassBPositionReport') or {})

        sog = pos.get('Sog', 0)
        cog = pos.get('Cog', 0)
        nav_status = pos.get('NavigationalStatus', 15)
        ship_type = pos.get('ShipType')

        cot_type = get_vessel_cot_type(ship_type)
        cot_xml = build_vessel_cot(mmsi, cot_type, lat, lon, sog, cog,
                                   ship_name, nav_status, self.stale_seconds)
        self.cot_sender.send(cot_xml)

        self.status.update({
            'ok': True,
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': self.status['last_count'] + 1
        })
        log.info('AIS vessel: ' + str(ship_name or mmsi) + ' at ' + str(round(lat,4)) + ',' + str(round(lon,4)))
