#!/usr/bin/env python3
"""
nifc_feed.py
NIFC wildfire perimeters + incidents → CoT markers and shapes
"""

import sys
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

from xml_utils import xml_attr, xml_text

log = logging.getLogger('nifc_feed')

NIFC_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"
            "/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")

def cot_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def cot_stale(hours=12):
    return (datetime.now(timezone.utc) + timedelta(hours=hours))\
           .strftime('%Y-%m-%dT%H:%M:%S.000Z')

def build_fire_point(uid, lat, lon, name, acres, pct_contained):
    now = cot_now()
    stale = cot_stale(12)
    pct = f"{pct_contained:.0f}%" if pct_contained is not None else "Unknown"
    callsign = f"FIRE: {name[:30]}"
    remarks = f"Wildfire: {name} | {acres:.0f} acres | {pct} contained\nSource: NIFC WFIGS"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid={xml_attr(uid)} type="b-m-p-s-f"
       how="h-g-i-g-o" time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="999999" le="999999"/>
  <detail>
    <contact callsign={xml_attr(callsign)}/>
    <remarks>{xml_text(remarks)}</remarks>
    <color argb="-23296"/>
  </detail>
</event>'''

def centroid_of_polygon(coords):
    """Calculate centroid of a polygon ring."""
    if not coords:
        return None, None
    lats = [c[1] for c in coords if len(c) >= 2]
    lons = [c[0] for c in coords if len(c) >= 2]
    if not lats:
        return None, None
    return sum(lats)/len(lats), sum(lons)/len(lons)

class NIFCFeed:
    def __init__(self, config, cot_sender, dedup):
        self.config = config
        self.nifc_config = config.get('feeds', {}).get('wildfire', {})
        self.cot_sender = cot_sender
        self.dedup = dedup
        self.poll_interval = self.nifc_config.get('poll_interval', 900)
        self.stale_hours = config.get('stale_times', {}).get('wildfire', 43200) // 3600
        self.status = {'ok': False, 'last_success': None,
                       'last_count': 0, 'errors': 0, 'last_error': ''}
        self._backoff = 1

        # Use extended area for wildfire — fires can approach from anywhere
        area = config.get('extended_area', config['area_of_interest'])
        self.bbox = f"{area['west']},{area['south']},{area['east']},{area['north']}"

    def run(self):
        log.info(f"NIFC wildfire feed starting (interval: {self.poll_interval}s)")
        while True:
            try:
                self._poll()
                self._backoff = 1
            except Exception as e:
                self._handle_error(str(e))
            time.sleep(self.poll_interval)

    def _poll(self):
        params = {
            'where': '1=1',
            'geometry': self.bbox,
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'IncidentName,GISAcres,PercentContained,'
                        'FireDiscoveryDateTime,ModifiedOnDateTime',
            'outSR': '4326',
            'f': 'geojson'
        }
        resp = requests.get(NIFC_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        count = 0
        for feature in data.get('features', []):
            props = feature.get('properties', {})
            geom = feature.get('geometry', {})

            name = props.get('IncidentName', 'Unknown Fire')
            acres = props.get('GISAcres', 0) or 0
            pct = props.get('PercentContained')
            modified = props.get('ModifiedOnDateTime', 0) or 0

            uid = f"nifc-{name.replace(' ', '_').lower()}-{int(modified/1000)}"

            if self.dedup.seen(uid, ttl_seconds=self.poll_interval * 2):
                continue

            # Get centroid for point marker
            gtype = geom.get('type', '')
            coords = geom.get('coordinates', [])
            lat, lon = None, None

            if gtype == 'Polygon' and coords:
                lat, lon = centroid_of_polygon(coords[0])
            elif gtype == 'MultiPolygon' and coords:
                lat, lon = centroid_of_polygon(coords[0][0])
            elif gtype == 'Point' and coords:
                lon, lat = coords[0], coords[1]

            if lat is None or lon is None:
                continue

            cot_xml = build_fire_point(uid, lat, lon, name, acres, pct)
            self.cot_sender.send(cot_xml)
            log.info(f"NIFC fire: {name} ({acres:.0f} acres, {pct}% contained)")
            count += 1

        self.status.update({'ok': True,
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': count})
        if count:
            log.info(f"NIFC: {count} active fires in area")

    def _handle_error(self, msg):
        self.status['errors'] += 1
        self.status['last_error'] = msg
        log.error(f"NIFC error: {msg}")
