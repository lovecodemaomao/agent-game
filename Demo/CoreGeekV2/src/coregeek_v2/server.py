import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .runtime import Agent
from .execution import ResponseBuilder


def make_server(port, agent=None, host='0.0.0.0'):
    agent = agent or Agent.production()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                response = agent.respond(payload)
            except Exception:
                logging.getLogger(__name__).exception('request failed')
                response = ResponseBuilder().build({}, ('', ''))
            body = json.dumps(response, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(port):
    with make_server(port) as server:
        server.serve_forever()
