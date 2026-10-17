"""Strict JSON parsing and redirect rejection for server-side AMD requests."""
import json
from urllib.request import HTTPRedirectHandler


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
