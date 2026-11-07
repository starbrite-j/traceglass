"""Loopback server admission control. This is not remote authentication."""
import threading
from http.server import ThreadingHTTPServer as StandardServer


class BoundedServer(StandardServer):
    max_connections = 16
    max_queries = 4
    connection_timeout = 15
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        if args and args[0][0] not in ('127.0.0.1','localhost'):
            raise ValueError('Only loopback binding is supported')
        self.connection_slots = threading.BoundedSemaphore(self.max_connections)
        self.query_slots = threading.BoundedSemaphore(self.max_queries)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.connection_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nRetry-After: 1\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request,address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(self.connection_timeout)
            super().process_request_thread(request,address)
        finally:
            self.connection_slots.release()
