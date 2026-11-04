"""A fake OpenAI-compatible vision server, for the OCR tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOcrServer:
    """reply(request_number, payload) -> (status, content) or (status, content, finish_reason):
    what the next request gets."""

    def __init__(self, reply=None):
        self.requests = []
        self.reply = reply or (lambda n, payload: (200, f"OCR text {n}"))
        server = self

        class Handler(BaseHTTPRequestHandler):
            def stream(self, content: str, finish: str) -> None:
                """Server-sent events, a few characters at a time, as vLLM streams. A client that
                stops reading closes the connection."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [{"choices": [{"delta": {"content": content[i:i + 20]}, "finish_reason": None}]}
                          for i in range(0, len(content), 20)]
                events += [{"choices": [{"delta": {}, "finish_reason": finish}]},
                           {"choices": [], "usage": {"completion_tokens": 7}}]
                try:
                    for event in events:
                        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                except OSError:
                    pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    server.requests.append(body)
                    n = len(server.requests)
                status, content, *finish = server.reply(n, body)
                if status == 200 and body.get("stream"):
                    self.stream(content, finish[0] if finish else "stop")
                    return
                if status == 200:
                    out = json.dumps({"choices": [{"message": {"content": content},
                                                   "finish_reason": finish[0] if finish else "stop"}],
                                      "usage": {"completion_tokens": 7}}).encode()
                else:
                    out = b'{"error": "nope"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port_url() -> str:
    """A URL nothing listens on."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"
