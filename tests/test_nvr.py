from builtins import TimeoutError as BuiltinTimeoutError
from secrets import token_urlsafe
from socket import gaierror
from ssl import SSLCertVerificationError
from urllib.error import URLError

import pytest
from vigi import (
    AuthenticationError,
    TransportError,
)
from vigi import (
    ConnectionError as SdkConnectionError,
)
from vigi import (
    TimeoutError as SdkTimeoutError,
)

from vigi_vision.nvr import NvrErrorKind, diagnose_nvr_error

_TEST_SECRET = token_urlsafe()


def _connection_error(cause: BaseException) -> SdkConnectionError:
    error = SdkConnectionError("connection failure")
    error.__cause__ = URLError(cause)
    return error


@pytest.mark.parametrize(
    ("error", "expected_kind", "expected_type"),
    [
        (
            AuthenticationError("authentication failed"),
            NvrErrorKind.AUTHENTICATION,
            "AuthenticationError",
        ),
        (SdkTimeoutError("request timed out"), NvrErrorKind.TIMEOUT, "TimeoutError"),
        (
            _connection_error(SSLCertVerificationError(1, "certificate")),
            NvrErrorKind.TLS_VERIFICATION,
            "SSLCertVerificationError",
        ),
        (
            _connection_error(BuiltinTimeoutError()),
            NvrErrorKind.TIMEOUT,
            "TimeoutError",
        ),
        (
            _connection_error(ConnectionRefusedError()),
            NvrErrorKind.CONNECTION_REFUSED,
            "ConnectionRefusedError",
        ),
        (
            _connection_error(gaierror(8, "host not found")),
            NvrErrorKind.HOST_RESOLUTION,
            "gaierror",
        ),
        (TransportError("request failed"), NvrErrorKind.SDK_REQUEST, "TransportError"),
        (RuntimeError(_TEST_SECRET), NvrErrorKind.UNEXPECTED, "RuntimeError"),
    ],
)
def test_diagnose_nvr_error_returns_redacted_causal_category(
    error: BaseException,
    expected_kind: NvrErrorKind,
    expected_type: str,
) -> None:
    # Given

    # When
    diagnostic = diagnose_nvr_error(error)

    # Then
    assert diagnostic.kind is expected_kind
    assert diagnostic.exception_type == expected_type
    assert _TEST_SECRET not in str(diagnostic)
