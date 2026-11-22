"""Allowlisted error diagnostics for the experimental vision transport.

Never return provider messages, response bodies, URLs, headers, or secrets.
A status is recorded only if exposed as a valid HTTP status code.
"""
from __future__ import annotations

import ssl

import httpx
import openai


class ProviderResponseError(ValueError):
    """A structurally unusable provider response; only a fixed code is kept."""
    _CODES = {'provider_refusal', 'empty_response', 'invalid_provider_response'}

    def __init__(self, kind: str):
        if kind not in self._CODES:
            raise ValueError('invalid diagnostic kind')
        super().__init__(kind)
        self.kind = kind


def classify_exception(exc: BaseException) -> str:
    """Return a fixed category or a bounded HTTP code, never exception text."""
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return f'http_{status}'
    if isinstance(exc, ProviderResponseError):
        return exc.kind if exc.kind in ProviderResponseError._CODES else 'other'
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return 'timeout'
    if isinstance(exc, (openai.APIConnectionError, httpx.NetworkError, ssl.SSLError, ConnectionError)):
        return 'connection_error'
    return 'other'


def describe_failure(kind: str) -> str:
    """Short Russian label for the journal; no interpretation of HTTP bodies."""
    if kind.startswith('http_') and kind[5:].isdigit() and 100 <= int(kind[5:]) <= 599:
        return f'HTTP {kind[5:]}'
    return {'timeout': 'тайм-аут', 'connection_error': 'ошибка соединения',
            'provider_refusal': 'отказ модели', 'empty_response': 'пустой ответ модели',
            'invalid_provider_response': 'неверный формат ответа провайдера',
            'other': 'причину определить не удалось'}.get(kind, 'причину определить не удалось')
