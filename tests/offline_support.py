"""Minimal HTTP and clock doubles; tests never access the network."""
from types import SimpleNamespace

class HTTPError(Exception):
    pass


class HTTPStatusError(HTTPError):
    def __init__(self, code, data):
        self.response = SimpleNamespace(status_code=code, json=lambda: data)


transport_error = type('TransportError', (HTTPError,), {})
fake_httpx = SimpleNamespace(HTTPError=HTTPError, HTTPStatusError=HTTPStatusError, TransportError=transport_error)


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
