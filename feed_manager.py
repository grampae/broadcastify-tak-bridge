#!/usr/bin/env python3
"""
feed_manager.py
Manpack CoT Feed Manager — orchestrates all automated feeds

Feeds:
  REST (threaded timer loops): ADS-B, NWS, NIFC, S2 Underground
  Stream (persistent connections): AIS (WebSocket), APRS (TCP)
  Audio stream + STT: Broadcastify

Usage:
  python3 feed_manager.py
  python3 feed_manager.py --config /path/to/feeds.yml
  python3 feed_manager.py --feeds adsb nws  (run specific feeds only)
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
import threading
from pathlib import Path
from datetime import datetime, timezone

###############################################################################
# LOGGING
###############################################################################

LOG_DIR = Path('/app/logs')
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / 'feeds.log')
    ]
)
log = logging.getLogger('feed_manager')

###############################################################################
# LOAD CONFIG
###############################################################################

def load_config(config_path):
    import yaml
    with open(config_path) as f:
        config = yaml.safe_load(f)
    log.info(f"Config loaded from {config_path}")
    return config

###############################################################################
# IMPORTS
###############################################################################

def import_feeds(feeds_dir):
    """Dynamically import feed modules."""
    sys.path.insert(0, str(feeds_dir))
    modules = {}
    feed_files = {
        'adsb':   'adsb_feed',
        'vessel': 'ais_feed',
        'weather': 'nws_feed',
        'wildfire': 'nifc_feed',
        'aprs':   'aprs_feed',
        's2_underground': 's2_feed',
        'broadcastify': 'broadcastify_feed',
    }
    for key, module_name in feed_files.items():
        try:
            modules[key] = __import__(module_name)
            log.debug(f"Imported feed module: {module_name}")
        except ImportError as e:
            log.warning(f"Could not import {module_name}: {e}")
    return modules

###############################################################################
# DEDUP CACHE (imported from dedup.py)
###############################################################################

import time as _time

class DedupCache:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def seen(self, uid, ttl_seconds=300):
        now = _time.time()
        with self._lock:
            # Clean expired
            expired = [k for k, v in self._cache.items() if v < now]
            for k in expired:
                del self._cache[k]
            if uid in self._cache:
                return True
            self._cache[uid] = now + ttl_seconds
            return False

    def invalidate(self, uid):
        with self._lock:
            self._cache.pop(uid, None)

    def size(self):
        with self._lock:
            return len(self._cache)

###############################################################################
# COT SENDER
###############################################################################

from cot_sender import COTSender

###############################################################################
# STATUS WRITER
###############################################################################

STATUS_FILE = Path('/app/data/feed_status.json')

def write_status(feed_statuses):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATUS_FILE.write_text(json.dumps(feed_statuses, indent=2))
    except Exception as e:
        log.debug(f"Status write error: {e}")

###############################################################################
# FEED RUNNER HELPERS
###############################################################################

def run_feed_in_thread(feed_obj, name):
    """Run a feed's .run() method in a daemon thread."""
    def wrapper():
        while True:
            try:
                log.info(f"Starting feed: {name}")
                feed_obj.run()
            except Exception as e:
                log.error(f"Feed {name} crashed: {e} — restarting in 60s")
                time.sleep(60)

    t = threading.Thread(target=wrapper, name=f"feed-{name}", daemon=True)
    t.start()
    return t

###############################################################################
# DASHBOARD STATUS UPDATER
###############################################################################

def status_updater_loop(feed_objects, interval=30):
    """Periodically write feed statuses to disk for dashboard."""
    while True:
        statuses = {}
        for name, feed_obj in feed_objects.items():
            statuses[name] = getattr(feed_obj, 'status',
                                     {'ok': False, 'note': 'no status'})
        statuses['_updated'] = datetime.now(timezone.utc).isoformat()
        statuses['_dedup_size'] = 0  # updated by main
        write_status(statuses)
        time.sleep(interval)

###############################################################################
# DASHBOARD SERVER
###############################################################################

def start_dashboard(config, netbird_ip):
    """Start the feed health dashboard on Netbird interface."""
    try:
        from dashboard.server import run_dashboard
        host = netbird_ip or '127.0.0.1'
        port = 8080
        t = threading.Thread(
            target=run_dashboard,
            args=(host, port),
            daemon=True,
            name='dashboard'
        )
        t.start()
        log.info(f"Dashboard: http://{host}:{port}/feeds")
    except Exception as e:
        log.warning(f"Dashboard failed to start: {e}")

###############################################################################
# MAIN
###############################################################################

def main():
    parser = argparse.ArgumentParser(description='Manpack CoT Feed Manager')
    parser.add_argument('--config', default='/app/feeds.yml')
    parser.add_argument('--feeds', nargs='+', help='Run only specific feeds')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    feeds_dir = Path(__file__).parent / 'feeds'
    modules = import_feeds(feeds_dir)

    # Initialize shared components
    dedup = DedupCache()
    sender = COTSender(config)

    netbird_ip = config.get('netbird_ip', '127.0.0.1')

    # Start dashboard
    start_dashboard(config, netbird_ip)

    # Feed class mapping
    feed_classes = {
        'adsb':          ('ADSBFeed',        'adsb'),
        'vessel':        ('AISFeed',         'vessel'),
        'weather':       ('NWSFeed',         'weather'),
        'wildfire':      ('NIFCFeed',        'wildfire'),
        'aprs':          ('APRSFeed',        'aprs'),
        's2_underground':('S2Feed',          's2_underground'),
        'broadcastify':  ('BroadcastifyFeed','broadcastify'),
    }

    module_map = {
        'adsb':          modules.get('adsb'),
        'vessel':        modules.get('vessel'),
        'weather':       modules.get('weather'),
        'wildfire':      modules.get('wildfire'),
        'aprs':          modules.get('aprs'),
        's2_underground':modules.get('s2_underground'),
        'broadcastify':  modules.get('broadcastify'),
    }

    feed_objects = {}
    threads = []

    feeds_config = config.get('feeds', {})

    # Load shared Whisper model if any broadcastify feed is enabled
    shared_whisper = None
    for feed_key, feed_cfg in feeds_config.items():
        is_broadcastify = (
            feed_key == 'broadcastify' or
            feed_cfg.get('type') == 'broadcastify'
        )
        if is_broadcastify and feed_cfg.get('enabled', False):
            try:
                broadcastify_module = modules.get('broadcastify')
                WhisperSTT = getattr(broadcastify_module, 'WhisperSTT')
                model_size = feed_cfg.get('whisper_model', 'medium.en')
                log.info(f"Loading shared Whisper model: {model_size}")
                shared_whisper = WhisperSTT(
                    model_size=model_size,
                    device='cpu',
                    compute_type='int8'
                )
                log.info("Shared Whisper model ready")
                break
            except Exception as e:
                log.error(f"Failed to load shared Whisper model: {e}")

    # Start standard feeds
    for feed_key, (class_name, config_key) in feed_classes.items():
        if feed_key == 'broadcastify':
            continue  # handled separately below

        feed_cfg = feeds_config.get(config_key, {})
        if not feed_cfg.get('enabled', False):
            log.info(f"Feed disabled: {feed_key}")
            continue

        if args.feeds and feed_key not in args.feeds:
            continue

        module = module_map.get(feed_key)
        if not module:
            log.warning(f"Module not loaded for feed: {feed_key}")
            continue

        feed_class = getattr(module, class_name, None)
        if not feed_class:
            log.warning(f"Class {class_name} not found in module for {feed_key}")
            continue

        try:
            feed_obj = feed_class(config, sender, dedup)
            feed_objects[feed_key] = feed_obj
            t = run_feed_in_thread(feed_obj, feed_key)
            threads.append(t)
            log.info(f"Feed started: {feed_key}")
        except Exception as e:
            log.error(f"Failed to start feed {feed_key}: {e}")

    # Start broadcastify feeds — supports multiple instances with shared Whisper
    broadcastify_module = modules.get('broadcastify')
    if broadcastify_module:
        BroadcastifyFeed = getattr(broadcastify_module, 'BroadcastifyFeed', None)
        for feed_key, feed_cfg in feeds_config.items():
            is_broadcastify = (
                feed_key == 'broadcastify' or
                feed_cfg.get('type') == 'broadcastify'
            )
            if not is_broadcastify:
                continue
            if not feed_cfg.get('enabled', False):
                log.info(f"Feed disabled: {feed_key}")
                continue
            if args.feeds and feed_key not in args.feeds:
                continue

            # Build per-feed config — merge global config with feed-specific overrides
            feed_config = dict(config)
            feed_config['feeds'] = dict(config.get('feeds', {}))
            feed_config['feeds']['broadcastify'] = feed_cfg

            # Per-feed area_of_interest override
            if 'area_of_interest' in feed_cfg:
                feed_config['area_of_interest'] = feed_cfg['area_of_interest']

            try:
                feed_obj = BroadcastifyFeed(
                    feed_config, sender, dedup,
                    whisper=shared_whisper
                )
                feed_objects[feed_key] = feed_obj
                t = run_feed_in_thread(feed_obj, feed_key)
                threads.append(t)
                log.info(f"Feed started: {feed_key}")
            except Exception as e:
                log.error(f"Failed to start feed {feed_key}: {e}")

    if not feed_objects:
        log.error("No feeds started — check feeds.yml enabled flags")
        sys.exit(1)

    log.info(f"Feed manager running — {len(feed_objects)} feeds active")
    log.info(f"Dashboard: http://{netbird_ip}:8080/feeds")

    # Start status updater
    status_thread = threading.Thread(
        target=status_updater_loop,
        args=(feed_objects,),
        daemon=True,
        name='status-updater'
    )
    status_thread.start()

    # Handle shutdown gracefully
    def shutdown(sig, frame):
        log.info("Shutting down feed manager...")
        write_status({k: v.status for k, v in feed_objects.items()})
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Keep main thread alive and log periodic stats
    while True:
        time.sleep(300)
        log.info(f"Feed manager alive — dedup cache: {dedup.size()} entries, "
                 f"TAK queue: {sender._send_queue.qsize()} pending")
        for name, feed in feed_objects.items():
            s = feed.status
            log.info(f"  {name}: ok={s.get('ok')} count={s.get('last_count')} "
                     f"errors={s.get('errors')}")

if __name__ == '__main__':
    main()
