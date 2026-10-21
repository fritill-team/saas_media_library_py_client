from __future__ import annotations

import copy
import pickle

import pytest

from pkg_media import (
    MediaLibraryError,
    NotFoundError,
    ServerError,
    UploadRejected,
    ValidationError,
)


def _pickled(exc):
    return pickle.loads(pickle.dumps(exc))


ROUND_TRIPS = [_pickled, copy.copy, copy.deepcopy]


@pytest.mark.parametrize("round_trip", ROUND_TRIPS, ids=["pickle", "copy", "deepcopy"])
def test_upload_rejected_survives_pickle_and_copy(round_trip) -> None:
    exc = UploadRejected("MIME type image/gif is not allowed", reason=UploadRejected.MIME_TYPE)

    restored = round_trip(exc)

    assert type(restored) is UploadRejected
    assert isinstance(restored, ValidationError)
    assert str(restored) == str(exc)
    assert restored.args == exc.args
    assert restored.reason == UploadRejected.MIME_TYPE
    assert restored.status_code is None and restored.body is None


@pytest.mark.parametrize("round_trip", ROUND_TRIPS, ids=["pickle", "copy", "deepcopy"])
@pytest.mark.parametrize("cls", [MediaLibraryError, NotFoundError, ServerError])
def test_http_errors_survive_pickle_and_copy(round_trip, cls) -> None:
    exc = cls("boom", status_code=503, body={"detail": "boom"})

    restored = round_trip(exc)

    assert type(restored) is cls
    assert str(restored) == "boom"
    assert restored.status_code == 503
    assert restored.body == {"detail": "boom"}


def test_restored_exception_can_be_raised_and_caught() -> None:
    restored = _pickled(UploadRejected("too big", reason=UploadRejected.TOO_LARGE))

    with pytest.raises(UploadRejected) as excinfo:
        raise restored

    assert excinfo.value.reason == UploadRejected.TOO_LARGE


@pytest.mark.parametrize("round_trip", ROUND_TRIPS, ids=["pickle", "copy", "deepcopy"])
def test_reason_survives_pickle_and_copy(round_trip) -> None:
    exc = NotFoundError("gone", status_code=404, body={}, reason="deploy_not_found")

    assert round_trip(exc).reason == "deploy_not_found"


def test_reason_defaults_to_none() -> None:
    assert MediaLibraryError("boom", status_code=500).reason is None
