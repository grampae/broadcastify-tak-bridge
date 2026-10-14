#!/usr/bin/env python3
"""
s2_feed.py
S2 Underground CIP/COP → CoT markers
Polls ArcGIS REST API from nexus-s2underground.hub.arcgis.com
"""

import sys
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

from xml_utils import xml_attr, xml_text

log = logging.getLogger('s2_feed')

###############################################################################
# S2 INCIDENT TYPE → COT TYPE
# Schema may vary — these are best-effort mappings
###############################################################################

S2_COT_MAP = {
    'ied':          ('b-m-p-w',         -65536),    # red
    'explosion':    ('b-m-p-w',         -65536),
    'fire':         ('b-m-p-s-f',       -23296),    # orange
    'riot':         ('b-m-p-g-i',       -65281),    # magenta
    'protest':      ('b-m-p-g-i',       -65281),
    'shooting':     ('b-m-p-w',         -65536),
    'hazmat':       ('b-m-p-w-CBRN',    -16711936), # green
    'chemical':     ('b-m-p-w-CBRN',    -16711936),
    'biological':   ('b-m-p-w-CBRN',    -16711936),
    'nuclear':      ('b-m-p-w-CBRN',    -16711936),
    'infrastructure': ('b-m-p-s',       -256),       # yellow
    'power':        ('b-m-p-s',         -256),
    'flood':        ('b-m-p-w-FLOOD',   -16711681), # blue
    'earthquake':   ('b-m-p-w',         -23296),
    'missing':      ('b-m-p-s',         -256),
    'safe':         ('b-m-p-s',         -16711936), # green = safe zone
}

DEFAULT_COT = ('b-m-p-s', -256)  # yellow generic safety

def get_s2_cot_type(properties):
    """Try to determine CoT type from S2 feature properties."""
    # S2 schema uses various field names — try common ones
    for field in ['incident_type', 'Category', 'Type', 'EventType',
                  'type', 'category', 'event_type']:
        val = properties.get(field, '')
        if val:
            val_lower = str(val).lower()
            for key, cot_info in S2_COT_MAP.items():
                if key in val_lower:
                    return cot_info
    return DEFAULT_COT

###############################################################################
# COT XML
###############################################################################

def cot_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_stale(seconds=7200):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds))\
           .strftime('%Y-%m-%dT%H:%M:%S.000Z')

def build_s2_cot(uid, cot_type, lat, lon, title, description,
                 stale_seconds=7200, color=-256):
    now = cot_now()
    stale = cot_stale(stale_seconds)
    desc_clean = xml_text(str(description or '')[:400])
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid={xml_attr(uid)} type={xml_attr(cot_type)}
       how="h-g-i-g-o" time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="999999" le="999999"/>
  <detail>
    <contact callsign={xml_attr(str(title)[:50])}/>
    <remarks>S2 Underground: {desc_clean}</remarks>
    <color argb="{color}"/>
  </detail>
</event>'''

###############################################################################
# ARCGIS SERVICE DISCOVERY
###############################################################################

def discover_s2_layer(base_url, service_name):
    """
    Try to find the correct feature service layer URL.
    S2 layer IDs may change — discover dynamically.
    """
    service_url = f"{base_url}/{service_name}"
    try:
        resp = requests.get(f"{service_url}?f=json", timeout=15)
        data = resp.json()
        layers = data.get('layers', [])
        if layers:
            # Return first layer with features
            return f"{service_url}/{layers[0]['id']}/query"
    except Exception as e:
        log.debug(f"S2 service discovery error: {e}")
    return f"{service_url}/0/query"  # fallback to layer 0

###############################################################################
# MAIN FEED CLASS
###############################################################################

class S2Feed:
    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.s2_config = config.get('feeds', {}).get('s2_underground', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.poll_interval = self.s2_config.get('poll_interval', 900)
        self.stale_seconds = config.get('stale_times', {}).get('s2', 7200)
        self.fail_silently = self.s2_config.get('fail_silently', True)
        self.cache_on_failure = self.s2_config.get('cache_on_failure', True)
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        self._backoff = 1
        self._cached_features = []
        self._query_url = None

        # Use extended area
        area = config.get('extended_area', config['area_of_interest'])
        self.bbox = f"{area['west']},{area['south']},{area['east']},{area['north']}"

        # Build query URL
        base = self.s2_config.get('base_url',
               'https://services.arcgis.com/DO4gTjwJVIJ7O9Ca/arcgis/rest/services')
        service = self.s2_config.get('service',
                  'S2_Underground_CIP_COP_Main_BaseData/FeatureServer')
        layer_id = self.s2_config.get('layer_id', 0)
        self._query_url = f"{base}/{service}/{layer_id}/query"

    def run(self):
        log.info(f"S2 Underground feed starting (interval: {self.poll_interval}s)")

        # Discover layer structure on first run
        self._discover_layer()

        while True:
            try:
                self._poll()
                self._backoff = 1
            except Exception as e:
                self._handle_error(str(e))
                if self.cache_on_failure and self._cached_features:
                    log.info("S2 using cached data")
                    self._push_features(self._cached_features)
            time.sleep(self.poll_interval)

    def _discover_layer(self):
        """Verify layer URL is accessible, log schema for debugging."""
        try:
            resp = requests.get(
                self._query_url.replace('/query', '') + '?f=json',
                timeout=10
            )
            data = resp.json()
            fields = [f['name'] for f in data.get('fields', [])]
            log.info(f"S2 layer fields: {fields}")
            # Store fields for property mapping
            self._fields = fields
        except Exception as e:
            log.warning(f"S2 layer discovery failed: {e} — will try anyway")
            self._fields = []

    def _poll(self):
        params = {
            'where': '1=1',
            'geometry': self.bbox,
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'outSR': '4326',
            'f': 'geojson',
            'resultRecordCount': 500,
        }
        resp = requests.get(self._query_url, params=params, timeout=30)

        # If 400/404, layer ID may have changed — try to rediscover
        if resp.status_code in [400, 404]:
            log.warning(f"S2 layer returned {resp.status_code} — attempting rediscovery")
            self._discover_layer()
            raise Exception(f"S2 HTTP {resp.status_code}")

        resp.raise_for_status()
        data = resp.json()
        features = data.get('features', [])
        self._cached_features = features
        self._push_features(features)

    def _push_features(self, features):
        count = 0
        for feature in features:
            props = feature.get('properties', {})
            geom = feature.get('geometry', {})

            # Extract coordinates
            gtype = geom.get('type', '')
            coords = geom.get('coordinates', [])
            lat, lon = None, None

            if gtype == 'Point' and len(coords) >= 2:
                lon, lat = coords[0], coords[1]
            elif gtype == 'MultiPoint' and coords:
                lon, lat = coords[0][0], coords[0][1]

            if lat is None or lon is None:
                continue
            if abs(lat) > 90 or abs(lon) > 180:
                continue

            # Build UID from whatever unique field is available
            uid_val = (props.get('OBJECTID') or props.get('objectid') or
                      props.get('id') or props.get('ID') or
                      f"{lat:.4f}_{lon:.4f}")
            uid = f"s2-{uid_val}"

            if self.dedup.seen(uid, ttl_seconds=self.stale_seconds):
                continue

            # Get title and description from common field names
            title = (props.get('title') or props.get('Title') or
                    props.get('name') or props.get('Name') or
                    props.get('EventType') or props.get('Category') or
                    'S2 Incident')

            description = (props.get('description') or props.get('Description') or
                          props.get('notes') or props.get('Notes') or
                          props.get('details') or props.get('Details') or '')

            cot_type, color = get_s2_cot_type(props)

            cot_xml = build_s2_cot(
                uid=uid,
                cot_type=cot_type,
                lat=lat,
                lon=lon,
                title=title,
                description=description,
                stale_seconds=self.stale_seconds,
                color=color
            )
            self.cot_sender.send(cot_xml)
            count += 1

        self.status.update({'ok': True,
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': count})
        if count:
            log.info(f"S2: {count} incidents in area")

    def _handle_error(self, msg):
        self.status['errors'] += 1
        self.status['last_error'] = msg
        if self.fail_silently:
            log.warning(f"S2 error (silent): {msg}")
        else:
            log.error(f"S2 error: {msg}")
