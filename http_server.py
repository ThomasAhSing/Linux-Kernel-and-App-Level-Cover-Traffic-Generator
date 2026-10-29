#!/usr/bin/env python3
"""No external assets, redirects, DNS, or dynamic content."""
from http.server import BaseHTTPRequestHandler, HTTPServer


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/page-two"):
            self.send_error(404)
            return
        body = (f"<!doctype html><title>Traffic testbed</title><h1>Lab page {self.path}</h1>"
                '<a href="/">First page</a> <a href="/page-two">Second page</a>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("10.203.0.2", 8000), Pages).serve_forever()
