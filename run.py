"""Koyeb entrypoint: tiny health-check HTTP server + the Telegram bot (polling)."""
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve_health():
    port = int(os.getenv("PORT", "8000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve_health, daemon=True).start()
    from bot import main
    asyncio.run(main())
