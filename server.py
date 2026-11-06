"""Local server for Live Quant Tracker.

Serves the page and relays stock / index data from Yahoo Finance (browsers can't
fetch it directly because of CORS). Standard library only.

    python server.py          -> opens http://localhost:8765/live-quant-tracker.html
"""
import http.server
import json
import os
import re
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOL_RE = re.compile(r"^[A-Za-z0-9^=.\-]{1,20}$")
INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d"}
RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}
# Fixed list of news feeds (keys only, so the server can't be used as an open proxy)
FEEDS = {
    "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EIXIC,%5ENDX,QQQ,NQ%3DF,MNQ%3DF&region=US&lang=en-US",
    "cnbc_markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "cnbc_tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # network error
        return 502, json.dumps({"error": str(e)}).encode()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/chart":
            q = urllib.parse.parse_qs(u.query)
            sym = q.get("symbol", [""])[0]
            iv = q.get("interval", ["15m"])[0]
            rg = q.get("range", ["1mo"])[0]
            if not SYMBOL_RE.match(sym) or iv not in INTERVALS or rg not in RANGES:
                return self.reply(400, b'{"error":"bad request"}')
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(sym)}?interval={iv}&range={rg}&includePrePost=false")
            return self.reply(*fetch(url))
        if u.path == "/api/rss":
            key = urllib.parse.parse_qs(u.query).get("feed", [""])[0]
            if key not in FEEDS:
                return self.reply(400, b'{"error":"unknown feed"}')
            code, body = fetch(FEEDS[key])
            return self.reply(code, body, "application/xml")
        return super().do_GET()

    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    url = f"http://localhost:{PORT}/live-quant-tracker.html"
    with Server(("127.0.0.1", PORT), Handler) as s:
        print(f"Live Quant Tracker running at {url}  (Ctrl+C to stop)")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        s.serve_forever()
