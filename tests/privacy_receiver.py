"""Real TLS receiver for synthetic privacy tests; no public network dependency."""
from contextlib import contextmanager
import json
from pathlib import Path
import ssl
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


@contextmanager
def receiver(responses=None):
    hits=[];responses=list(responses or ['ok'])
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0)))
            hits.append({'path':self.path,'headers':dict(self.headers),'body':body.decode('utf-8')})
            answer=responses[min(len(hits)-1,len(responses)-1)]
            if isinstance(answer,dict) and 'sse' in answer:
                data=answer['sse'].encode();kind='text/event-stream'
            else:
                data=json.dumps({'output_text':answer}).encode();kind='application/json'
            self.send_response(200);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    fixtures=Path(__file__).parent/'fixtures/privacy'
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixtures/'receiver-cert.pem',fixtures/'receiver-key.pem')
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'https://localhost:'+str(server.server_port),hits
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def trust_receiver(monkeypatch):
    # Only this explicit test CA is trusted. Hostname verification stays enabled.
    monkeypatch.setenv('SSL_CERT_FILE',str(Path(__file__).parent/'fixtures/privacy/receiver-cert.pem'))
