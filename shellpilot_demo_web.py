import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTH_BODY = b"shellpilot-demo-web: ok\n"


class DemoWeb(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            status, body = 200, HEALTH_BODY
        elif self.path == "/":
            status, body = 200, b"ShellPilot managed demo-web service\n"
        else:
            status, body = 404, b"Not found\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("SPT_BACKEND_PORT", "18081"))
    if not 1024 <= port <= 65535:
        raise SystemExit("SPT_BACKEND_PORT must be between 1024 and 65535")
    with ThreadingHTTPServer(("127.0.0.1", port), DemoWeb) as server:
        print(f"demo-web: http://127.0.0.1:{port}/health", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
