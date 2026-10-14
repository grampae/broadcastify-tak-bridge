import json
import time
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

STATUS_FILE = Path(__file__).parent.parent / 'data' / 'feed_status.json'

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ['/', '/feeds']:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            try:
                status = json.loads(STATUS_FILE.read_text())
            except:
                status = {}
            rows = ''
            for feed, data in status.items():
                if feed.startswith('_'):
                    continue
                ok = data.get('ok', False)
                color = '#2ecc71' if ok else '#e74c3c'
                feed_name = escape(str(feed))
                last_success = escape(str(data.get('last_success', 'never')))
                last_error = escape(str(data.get('last_error', ''))[:80])
                rows += f'<tr><td>{feed_name}</td><td style="color:{color}">{"OK" if ok else "ERROR"}</td><td>{last_success}</td><td>{data.get("last_count",0)}</td><td>{data.get("errors",0)}</td><td>{last_error}</td></tr>'
            html = f'<!DOCTYPE html><html><head><title>Feed Status</title><meta http-equiv="refresh" content="30"><style>body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;padding:20px}}h1{{color:#00ff88}}table{{width:100%;border-collapse:collapse}}th{{background:#16213e;padding:8px;text-align:left}}td{{padding:8px;border-bottom:1px solid #333}}</style></head><body><h1>TAK Feed Status</h1><p>{time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}</p><table><tr><th>Feed</th><th>Status</th><th>Last Success</th><th>Count</th><th>Errors</th><th>Last Error</th></tr>{rows}</table></body></html>'
            self.wfile.write(html.encode())
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                self.wfile.write(STATUS_FILE.read_bytes())
            except:
                self.wfile.write(b'{}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def run_dashboard(host='0.0.0.0', port=8080):
    server = HTTPServer((host, port), StatusHandler)
    print(f"Feed dashboard: http://{host}:{port}/feeds")
    server.serve_forever()
