from __future__ import annotations

import io
import json
import urllib.error

import pytest

from global_hybrid_v2.adapters.drive_xlsx_workbench import (
    DRIVE_WORKBENCH_SCOPE,
    GoogleDriveRestTransport,
    WorkbenchCapabilityDebt,
    WorkbenchClaimHttpTransport,
    WorkbenchConflict,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()

    def __iter__(self):
        return iter(self.read().splitlines(keepends=True))


def test_drive_transport_binds_exact_file_id_mime_and_bearer():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        url = request.full_url
        if "files?" in url and "alt=media" not in url:
            return Response({"files": [{"id": "drive-file-1"}]})
        if url.endswith("alt=media"):
            return Response(b"xlsx")
        return Response(
            {
                "id": "drive-file-1",
                "version": "7",
                "mimeType": GoogleDriveRestTransport.XLSX_MIME,
                "modifiedTime": "2026-09-27T00:00:00Z",
            }
        )

    transport = GoogleDriveRestTransport(lambda: "token", opener=opener)
    assert transport.metadata("drive-file-1")["version"] == "7"
    assert transport.download("drive-file-1") == b"xlsx"
    transport.replace("drive-file-1", b"new", GoogleDriveRestTransport.XLSX_MIME)
    assert all(call[0].headers["Authorization"] == "Bearer token" for call in calls)
    assert calls[-1][0].method == "PATCH"
    assert "/drive-file-1?" in calls[-1][0].full_url


def test_drive_transport_rejects_target_and_mime_mismatch():
    def wrong_id(_request, timeout):
        return Response({"id": "other", "version": "1", "mimeType": GoogleDriveRestTransport.XLSX_MIME})

    with pytest.raises(WorkbenchConflict, match="TARGET_ID_MISMATCH"):
        GoogleDriveRestTransport(lambda: "token", opener=wrong_id).metadata("drive-file-1")

    def wrong_mime(_request, timeout):
        return Response({"id": "drive-file-1", "version": "1", "mimeType": "text/plain"})

    with pytest.raises(WorkbenchConflict, match="TARGET_MIME_MISMATCH"):
        GoogleDriveRestTransport(lambda: "token", opener=wrong_mime).metadata("drive-file-1")


def test_drive_transport_replace_fails_closed_on_bad_visible_corpus():
    empty_case: dict = {"files": []}
    two_case: dict = {"files": [{"id": "a"}, {"id": "b"}]}
    wrong_id_case: dict = {"files": [{"id": "other"}]}

    for bad_response in (empty_case, two_case, wrong_id_case):
        calls = []

        def bad_opener(request, timeout):
            calls.append((request, timeout))
            url = request.full_url
            if "files?" in url and "alt=media" not in url:
                return Response(bad_response)
            if url.endswith("alt=media"):
                return Response(b"xlsx")
            return Response(
                {
                    "id": "drive-file-1",
                    "version": "7",
                    "mimeType": GoogleDriveRestTransport.XLSX_MIME,
                    "modifiedTime": "2026-09-27T00:00:00Z",
                }
            )

        transport = GoogleDriveRestTransport(lambda: "token", opener=bad_opener)
        with pytest.raises(WorkbenchConflict, match="HOLD_VISIBLE_CORPUS_NOT_EXACTLY_ONE_TARGET"):
            transport.replace("drive-file-1", b"new", GoogleDriveRestTransport.XLSX_MIME)
        patch_calls = [c for c in calls if c[0].method == "PATCH"]
        assert len(patch_calls) == 0


def test_claim_transport_uses_only_three_bounded_control_paths_and_bearer_secret():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return Response({"state": "CLAIMED"})

    transport = WorkbenchClaimHttpTransport(
        base_url="https://control.example",
        write_secret="secret",
        opener=opener,
    )
    transport.claim({"claim_id": "c"})
    transport.complete({"claim_id": "c"})
    transport.fail({"claim_id": "c"})
    assert [request.full_url for request in calls] == [
        "https://control.example/internal/control/workbench-write-claim",
        "https://control.example/internal/control/workbench-write-complete",
        "https://control.example/internal/control/workbench-write-fail",
    ]
    assert all(request.headers["Authorization"] == "Bearer secret" for request in calls)


def test_claim_http_error_fails_closed_without_secret_in_error():
    secret = "do-not-leak"

    def opener(request, timeout):
        body = io.BytesIO(b'{"blocker":"CONTROL_AUTH_REQUIRED"}')
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, body)

    transport = WorkbenchClaimHttpTransport(
        base_url="https://control.example",
        write_secret=secret,
        opener=opener,
    )
    with pytest.raises(WorkbenchCapabilityDebt) as exc:
        transport.claim({"claim_id": "c"})
    assert "CONTROL_AUTH_REQUIRED" in str(exc.value)
    assert secret not in str(exc.value)
