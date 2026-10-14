#!/usr/bin/env python3
"""
nws_feed.py
NWS weather alerts → CoT markers
Polls api.weather.gov for active alerts in configured Michigan zones
"""

import sys
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateutil_parser

from xml_utils import xml_attr, xml_text

log = logging.getLogger('nws_feed')

###############################################################################
# ALERT TYPE → COT TYPE + PRIORITY
###############################################################################

ALERT_COT_MAP = {
    'Tornado Warning':              ('b-m-p-w-TORNADO', 'critical', -65536),   # red
    'Tornado Watch':                ('b-m-p-w-TORNADO', 'high',     -32768),   # dark red
    'Severe Thunderstorm Warning':  ('b-m-p-w-TSTORM',  'high',     -32512),   # orange-red
    'Severe Thunderstorm Watch':    ('b-m-p-w-TSTORM',  'medium',   -23296),   # orange
    'Flash Flood Warning':          ('b-m-p-w-FLOOD',   'high',     -16711681),# blue
    'Flash Flood Watch':            ('b-m-p-w-FLOOD',   'medium',   -8388480), # dark blue
    'Winter Storm Warning':         ('b-m-p-w-WINTER',  'medium',   -1),       # white
    'Winter Storm Watch':           ('b-m-p-w-WINTER',  'low',      -8355712), # grey
    'Blizzard Warning':             ('b-m-p-w-WINTER',  'high',     -1),       # white
    'Ice Storm Warning':            ('b-m-p-w-WINTER',  'high',     -8355712), # grey
    'Extreme Cold Warning':         ('b-m-p-w',         'medium',   -16711681),# blue
    'Wind Advisory':                ('b-m-p-w',         'low',      -23296),   # orange
    'High Wind Warning':            ('b-m-p-w',         'medium',   -32512),   # orange-red
    'Special Weather Statement':    ('b-m-p-w',         'low',      -23296),
    'Flood Warning':                ('b-m-p-w-FLOOD',   'medium',   -16711681),
}

DEFAULT_COT = ('b-m-p-w', 'low', -23296)

###############################################################################
# COT XML
###############################################################################

def cot_time_from_str(s):
    """Parse ISO datetime string to CoT format."""
    try:
        dt = dateutil_parser.parse(s).astimezone(timezone.utc)
        return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    except:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def build_weather_cot(uid, cot_type, lat, lon, title, description,
                      expires_str, color_argb=-23296):
    now = cot_now()
    stale = cot_time_from_str(expires_str) if expires_str else \
            (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    desc_short = xml_text(description[:400]) if description else ''
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid={xml_attr(uid)} type={xml_attr(cot_type)}
       how="h-g-i-g-o" time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="999999" le="999999"/>
  <detail>
    <contact callsign={xml_attr(title[:50])}/>
    <remarks>{desc_short}</remarks>
    <color argb="{color_argb}"/>
  </detail>
</event>'''

###############################################################################
# ZONE CENTROID FALLBACK
###############################################################################

# NWS zone → approximate centroid for zones without polygon geometry
ZONE_CENTROIDS = {
    'MIZ001': (46.55, -87.40),  # Marquette County
    'MIZ002': (46.40, -86.60),  # Alger County
    'MIZ003': (46.75, -88.45),  # Baraga County
    'MIZ004': (46.15, -89.00),  # Iron County
    'MIZ005': (45.85, -88.00),  # Dickinson County
    'MIZ006': (46.70, -89.30),  # Ontonagon County
    'MIZ007': (47.12, -88.57),  # Houghton County
    'MIZ008': (47.40, -88.20),  # Keweenaw County
    'MIZ009': (46.45, -85.50),  # Luce County
    'MIZ010': (45.95, -86.10),  # Schoolcraft County
}

def get_alert_location(feature):
    """Extract lat/lon from alert feature. Falls back to zone centroid."""
    geometry = feature.get('geometry')
    if geometry:
        gtype = geometry.get('type')
        coords = geometry.get('coordinates', [])
        if gtype == 'Point' and coords:
            return coords[1], coords[0]
        if gtype in ['Polygon', 'MultiPolygon'] and coords:
            # Use centroid of first polygon
            ring = coords[0] if gtype == 'Polygon' else coords[0][0]
            if ring:
                lats = [c[1] for c in ring]
                lons = [c[0] for c in ring]
                return sum(lats)/len(lats), sum(lons)/len(lons)

    # Fallback: use zone centroid
    ugc_codes = feature.get('properties', {}).get('geocode', {}).get('UGC', [])
    for ugc in ugc_codes:
        if ugc in ZONE_CENTROIDS:
            return ZONE_CENTROIDS[ugc]

    # Last resort: Marquette County center
    return 46.55, -87.40

###############################################################################
# MAIN FEED CLASS
###############################################################################

class NWSFeed:
    BASE_URL = 'https://api.weather.gov/alerts/active'

    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.nws_config = config.get('feeds', {}).get('weather', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.poll_interval = self.nws_config.get('poll_interval', 300)
        self.user_agent = self.nws_config.get('user_agent',
                          'ManpackFeeds/1.0 (tactical@manpack.local)')
        self.zones = self.nws_config.get('zones', ['MIZ001'])
        self.alert_types = set(self.nws_config.get('alert_types', list(ALERT_COT_MAP.keys())))
        self.min_severity = self.nws_config.get('min_severity', 'Moderate')
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        self._backoff = 1
        self._cached = []

        SEVERITY_ORDER = ['Minor', 'Moderate', 'Severe', 'Extreme']
        self.min_severity_idx = SEVERITY_ORDER.index(self.min_severity) \
            if self.min_severity in SEVERITY_ORDER else 0

    def run(self):
        log.info(f"NWS feed starting (zones: {self.zones}, interval: {self.poll_interval}s)")
        while True:
            try:
                self._poll()
                self._backoff = 1
            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    wait = min(self._backoff * 300, 3600)
                    log.warning(f"NWS rate limited — waiting {wait}s")
                    time.sleep(wait)
                    self._backoff *= 2
                    continue
                else:
                    self._handle_error(str(e))
            except Exception as e:
                self._handle_error(str(e))
            time.sleep(self.poll_interval)

    def _poll(self):
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/geo+json'}
        # Query by state — filter by zone client-side
        resp = requests.get(f"{self.BASE_URL}?area=MI",
                           headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        SEVERITY_ORDER = ['Minor', 'Moderate', 'Severe', 'Extreme']
        count = 0
        for feature in data.get('features', []):
            props = feature.get('properties', {})

            # Zone filter
            ugc = props.get('geocode', {}).get('UGC', [])
            if not any(z in self.zones for z in ugc):
                continue

            # Alert type filter
            event = props.get('event', '')
            if self.alert_types and event not in self.alert_types:
                continue

            # Severity filter
            severity = props.get('severity', 'Unknown')
            if severity in SEVERITY_ORDER:
                if SEVERITY_ORDER.index(severity) < self.min_severity_idx:
                    continue

            alert_id = props.get('id', '')
            if not alert_id:
                continue

            # Dedup — use alert ID, TTL = alert duration
            expires = props.get('expires', '')
            ttl = 3600  # default 1 hour
            if expires:
                try:
                    exp_dt = dateutil_parser.parse(expires).astimezone(timezone.utc)
                    ttl = max(60, int((exp_dt - datetime.now(timezone.utc)).total_seconds()))
                except:
                    pass

            uid = f"nws-{alert_id.split('/')[-1]}"
            if self.dedup.seen(uid, ttl_seconds=ttl):
                continue

            lat, lon = get_alert_location(feature)
            cot_info = ALERT_COT_MAP.get(event, DEFAULT_COT)
            cot_type, priority, color = cot_info

            title = f"{event} — {props.get('areaDesc', '')[:40]}"
            description = props.get('description', props.get('headline', ''))

            cot_xml = build_weather_cot(
                uid=uid,
                cot_type=cot_type,
                lat=lat,
                lon=lon,
                title=title,
                description=description,
                expires_str=expires,
                color_argb=color
            )
            self.cot_sender.send(cot_xml)
            log.info(f"NWS alert: {event} ({severity}) — {props.get('areaDesc', '')[:50]}")
            count += 1

        self.status.update({'ok': True,
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': count})

    def _handle_error(self, msg):
        self.status['errors'] += 1
        self.status['last_error'] = msg
        log.error(f"NWS error: {msg}")
