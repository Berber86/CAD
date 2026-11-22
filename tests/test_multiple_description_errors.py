"""Only allowlisted diagnostic codes, never exception/provider content."""
import httpx
import openai
import pytest

from multiple_description_errors import ProviderResponseError, classify_exception, describe_failure


class Status(Exception):
    def __init__(self, code):
        super().__init__('SECRET provider response with document text')
        self.status_code = code


@pytest.mark.parametrize('exc,kind', [
    (Status(401), 'http_401'), (Status(429), 'http_429'), (Status(503), 'http_503'),
    (Status(True), 'other'), (Status(999), 'other'),
    (TimeoutError('SECRET'), 'timeout'), (httpx.ReadTimeout('SECRET'), 'timeout'),
    (openai.APITimeoutError(request=httpx.Request('POST', 'https://example.test')), 'timeout'),
    (httpx.ConnectError('SECRET'), 'connection_error'),
    (ProviderResponseError('empty_response'), 'empty_response'),
    (ValueError('SECRET'), 'other'),
])
def test_classify_is_explicit_and_never_reveals_raw_content(exc, kind):
    assert classify_exception(exc) == kind
    assert 'SECRET' not in describe_failure(kind)


def test_unknown_diagnostic_cannot_be_rendered_as_user_text():
    assert 'SECRET' not in describe_failure('SECRET')
    assert describe_failure('http_401') == 'HTTP 401'
