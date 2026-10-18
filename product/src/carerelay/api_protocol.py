"""Strict JSON, redirect rejection and metadata-only HTTP timing."""
from contextlib import contextmanager
from contextvars import ContextVar
from http.client import HTTPSConnection
import json
import time
from urllib.request import HTTPRedirectHandler, HTTPSHandler


TASK_ID = ContextVar('amd_task_id', default=None)


@contextmanager
def trace_task(identifier):
    token = TASK_ID.set(identifier)
    try:
        yield
    finally:
        TASK_ID.reset(token)


class _TimedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, timings, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = timings
        create_connection = self._create_connection

        def timed_connection(*args, **kwargs):
            self.timings['phase'] = 'dns_tcp'
            started = time.perf_counter()
            try:
                connection = create_connection(*args, **kwargs)
            finally:
                self.timings['dns_tcp_seconds'] = time.perf_counter() - started
            self.timings['phase'] = 'tls_tunnel'
            return connection

        self._create_connection = timed_connection

    def connect(self):
        started = time.perf_counter()
        try:
            super().connect()
        finally:
            self.timings['connection_seconds'] = time.perf_counter() - started
            # Includes a proxy CONNECT tunnel when one is configured.
            self.timings['tls_tunnel_seconds'] = max(0, self.timings['connection_seconds']
                                                     - self.timings.get('dns_tcp_seconds', 0))
        self.timings['phase'] = 'send'

    def send(self, data):
        self.timings['phase'] = 'send'
        connection_before = self.timings.get('connection_seconds', 0)
        started = time.perf_counter()
        try:
            return super().send(data)
        finally:
            connection = self.timings.get('connection_seconds', 0) - connection_before
            self.timings['send_seconds'] = self.timings.get('send_seconds', 0) + max(
                0, time.perf_counter() - started - connection)

    def getresponse(self):
        self.timings['phase'] = 'response_headers'
        started = time.perf_counter()
        try:
            return super().getresponse()
        finally:
            self.timings['response_headers_seconds'] = time.perf_counter() - started


class _TimedHTTPSHandler(HTTPSHandler):
    def __init__(self, timings):
        super().__init__()
        self.timings = timings

    def https_open(self, req):
        def connection(*args, **kwargs):
            return _TimedHTTPSConnection(*args, timings=self.timings, **kwargs)
        return self.do_open(connection, req, context=self._context)


class InferenceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InferenceError('redirect', 'The inference endpoint redirected; verify its configuration before retrying.')


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('Non-finite JSON number')

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
