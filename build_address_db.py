#!/usr/bin/env python3
"""
build_address_db.py
Downloads address data from public ArcGIS services and builds a local
SQLite database for fast offline address geocoding.

Supported services:
  --service marquette   Marquette County MI parcel data (polygon centroids)
  --service ri          Rhode Island E-911 Sites statewide (points)
  --service <URL>       Custom ArcGIS FeatureServer/0/query URL

Usage:
  python3 build_address_db.py --service marquette --output /app/data/marquette_addresses.db
  python3 build_address_db.py --service ri --output /app/data/ri_addresses.db
  python3 build_address_db.py --test "767 North Koski Lane" --output /app/data/marquette_addresses.db
"""

import re
import sys
import time
import sqlite3
import argparse
import requests
from pathlib import Path

###############################################################################
# SERVICE CONFIGS
###############################################################################

SERVICES = {
    'marquette': {
        'url': (
            'https://services9.arcgis.com/6EuFgO4fLTqfNOhu/ArcGIS/rest/services'
            '/MarquetteParcelData/FeatureServer/2/query'
        ),
        'geometry': 'polygon',
        'address_field': 'address',
        'house_field': 'll_address',
        'street_field': 'saddstr',
        'city_field': 'city',
        'where': "address IS NOT NULL AND address <> ''",
    },
    'ri': {
        'url': (
            'https://services2.arcgis.com/S8zZg9pg23JUEexQ/arcgis/rest/services'
            '/FACILITY_Sites_E911_24r1/FeatureServer/0/query'
        ),
        'geometry': 'point',
        'address_field': 'Add_Full',
        'house_field': 'AddNumFull',
        'street_field': 'St_Full',
        'city_field': 'MSAGComm',
        'where': "Add_Full IS NOT NULL AND Add_Full <> ' '",
    },
}

MAX_RECORDS = 2000

###############################################################################
# DOWNLOAD
###############################################################################

def download_addresses(service_config, output_db):
    url = service_config['url']
    geometry_type = service_config['geometry']
    addr_field = service_config['address_field']
    house_field = service_config['house_field']
    street_field = service_config['street_field']
    city_field = service_config['city_field']
    where = service_config['where']

    print(f"Downloading addresses from:\n  {url}")
    print(f"Output: {output_db}")

    conn = sqlite3.connect(output_db)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS addresses (
            id INTEGER PRIMARY KEY,
            address TEXT NOT NULL,
            house_number TEXT,
            street_name TEXT,
            city TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_address ON addresses(address)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_street ON addresses(street_name, house_number)')
    conn.commit()

    offset = 0
    total = 0

    while True:
        params = {
            'where': where,
            'outFields': f'{addr_field},{house_field},{street_field},{city_field}',
            'returnGeometry': 'true',
            'resultOffset': offset,
            'resultRecordCount': MAX_RECORDS,
            'outSR': '4326',
            'f': 'geojson'
        }

        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            time.sleep(5)
            continue

        features = data.get('features', [])
        if not features:
            break

        rows = []
        for feat in features:
            props = feat.get('properties', {})
            geom = feat.get('geometry', {})

            address = str(props.get(addr_field) or '').strip().upper()
            if not address or address in (' ', ''):
                continue

            house_num = str(props.get(house_field) or '').strip()
            street = str(props.get(street_field) or '').strip().upper()
            city = str(props.get(city_field) or '').strip()

            if geometry_type == 'point':
                coords = geom.get('coordinates', [])
                if not coords or len(coords) < 2:
                    continue
                lon, lat = coords[0], coords[1]
            else:
                lat, lon = polygon_centroid(geom)
                if lat is None:
                    continue

            if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
                continue

            rows.append((address, house_num, street, city, lat, lon))

        if rows:
            conn.executemany(
                'INSERT OR IGNORE INTO addresses (address, house_number, street_name, city, lat, lon) VALUES (?,?,?,?,?,?)',
                rows
            )
            conn.commit()

        total += len(rows)
        offset += MAX_RECORDS
        print(f"  Downloaded {total} addresses (offset {offset})...")
        time.sleep(0.1)

        if len(features) < MAX_RECORDS:
            break

    print(f"\nDone: {total} addresses in {output_db}")
    conn.close()
    return total


def polygon_centroid(geom):
    if not geom:
        return None, None
    gtype = geom.get('type', '')
    coords = geom.get('coordinates', [])
    try:
        if gtype == 'Polygon' and coords:
            ring = coords[0]
            lats = [c[1] for c in ring]
            lons = [c[0] for c in ring]
            return sum(lats)/len(lats), sum(lons)/len(lons)
        elif gtype == 'MultiPolygon' and coords:
            ring = coords[0][0]
            lats = [c[1] for c in ring]
            lons = [c[0] for c in ring]
            return sum(lats)/len(lats), sum(lons)/len(lons)
    except:
        pass
    return None, None

###############################################################################
# LOOKUP
###############################################################################

DIR_MAP = {
    'NORTH': 'N', 'SOUTH': 'S', 'EAST': 'E', 'WEST': 'W',
    'NORTHEAST': 'NE', 'NORTHWEST': 'NW', 'SOUTHEAST': 'SE', 'SOUTHWEST': 'SW',
}

STREET_TYPE_MAP = {
    'STREET': 'ST', 'AVENUE': 'AVE', 'ROAD': 'RD', 'DRIVE': 'DR',
    'BOULEVARD': 'BLVD', 'LANE': 'LN', 'COURT': 'CT', 'PLACE': 'PL',
    'WAY': 'WY', 'CIRCLE': 'CIR', 'TRAIL': 'TRL', 'HIGHWAY': 'HWY',
    'PARKWAY': 'PKWY', 'TERRACE': 'TER', 'WY': 'WY',
}

def normalize_street(street):
    words = street.upper().split()
    return ' '.join(DIR_MAP.get(w, STREET_TYPE_MAP.get(w, w)) for w in words)

def parse_address(address):
    m = re.match(r'^(\d+)\s+(.+)$', address.strip().upper())
    if m:
        return m.group(1), m.group(2).strip()
    return None, address.strip().upper()

def lookup_address(db_path, address_str):
    if not Path(db_path).exists():
        return None, None, None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    addr = address_str.strip().upper()
    addr_norm = normalize_street(addr)

    # Exact match
    row = conn.execute(
        'SELECT lat, lon, address FROM addresses WHERE address = ? LIMIT 1',
        (addr_norm,)
    ).fetchone()
    if row:
        conn.close()
        return row['lat'], row['lon'], row['address']

    # House number + street name match with direction stripping
    house, street = parse_address(addr)
    if house and street:
        street_norm = normalize_street(street)
        variants = [street_norm]
        words = street_norm.split()
        if words and words[0] in ('N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW'):
            variants.append(' '.join(words[1:]))

        for sv in variants:
            if not sv:
                continue
            row = conn.execute(
                '''SELECT lat, lon, address FROM addresses
                   WHERE house_number = ? AND street_name LIKE ? LIMIT 1''',
                (house, f'%{sv[:12]}%')
            ).fetchone()
            if row:
                conn.close()
                return row['lat'], row['lon'], row['address']

    # Street name only — return midpoint
    if street:
        street_norm = normalize_street(street)
        words = street_norm.split()
        if words and words[0] in ('N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW'):
            street_norm = ' '.join(words[1:])
        if street_norm:
            rows = conn.execute(
                'SELECT lat, lon, address FROM addresses WHERE street_name LIKE ? LIMIT 10',
                (f'%{street_norm[:12]}%',)
            ).fetchall()
            if rows:
                lat = sum(r['lat'] for r in rows) / len(rows)
                lon = sum(r['lon'] for r in rows) / len(rows)
                conn.close()
                return lat, lon, rows[0]['address'].split(' ', 1)[1] if rows else street

    conn.close()
    return None, None, None

###############################################################################
# ENTRY POINT
###############################################################################

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Address Database Builder')
    parser.add_argument('--service', default='marquette',
                        help='Service name (marquette, ri) or custom FeatureServer URL')
    parser.add_argument('--output', default='/app/data/marquette_addresses.db',
                        help='Output SQLite database path')
    parser.add_argument('--test', metavar='ADDRESS',
                        help='Test lookup of an address')
    args = parser.parse_args()

    if args.test:
        lat, lon, matched = lookup_address(args.output, args.test)
        if lat:
            print(f"Found: {matched}")
            print(f"  Lat: {lat:.6f}, Lon: {lon:.6f}")
        else:
            print(f"Not found: {args.test}")
    else:
        # Get service config
        if args.service in SERVICES:
            service_config = SERVICES[args.service]
        else:
            print(f"Unknown service: {args.service}")
            print(f"Available: {', '.join(SERVICES.keys())}")
            sys.exit(1)

        download_addresses(service_config, args.output)
