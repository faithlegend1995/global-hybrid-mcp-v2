from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class WorkbenchCapabilityDebt(RuntimeError): ...
class WorkbenchConflict(RuntimeError): ...
class WorkbenchPostwriteMismatch(RuntimeError): ...

DRIVE_WORKBENCH_SCOPE = "https://www.googleapis.com/auth/drive"


class DriveTransport(Protocol):
    def metadata(self, file_id: str) -> dict: ...
    def download(self, file_id: str) -> bytes: ...
    def replace(self, file_id: str, payload: bytes, mime_type: str) -> dict: ...


class ClaimTransport(Protocol):
    def claim(self, payload: dict) -> dict: ...
    def complete(self, payload: dict) -> dict: ...
    def fail(self, payload: dict) -> dict: ...


@dataclass(frozen=True)
class WorkbenchWriteReceipt:
    state: str
    file_id: str
    preimage_version: str
    preimage_sha256: str
    postwrite_version: str | None = None
    postwrite_sha256: str | None = None
    claim_id: str | None = None


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class GoogleDriveRestTransport:
    """Bounded raw-file Drive transport using a deployment-owned OAuth token."""

    FILES_BASE = "https://www.googleapis.com/drive/v3/files"
    UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __init__(
        self,
        access_token_provider: Callable[[], str],
        *,
        timeout: float = 15,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        self._access_token_provider = access_token_provider
        self.timeout = timeout
        self._opener = opener

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: bytes | None = None,
        content_type: str | None = None,
        expect_json: bool,
    ) -> dict | bytes:
        headers = {
            "authorization": f"Bearer {self._access_token_provider()}",
            "accept": "application/json",
        }
        if content_type is not None:
            headers["content-type"] = content_type
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                if expect_json:
                    result = json.load(response)
                    if not isinstance(result, dict):
                        raise WorkbenchCapabilityDebt("GOOGLE_DRIVE_RESPONSE_INVALID")
                    return result
                return response.read()
        except urllib.error.HTTPError as exc:
            raise WorkbenchCapabilityDebt(f"GOOGLE_DRIVE_HTTP_{exc.code}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchCapabilityDebt("GOOGLE_DRIVE_RESPONSE_INVALID") from exc

    @staticmethod
    def _quoted(file_id: str) -> str:
        if not isinstance(file_id, str) or not file_id.strip():
            raise WorkbenchConflict("WORKBENCH_FILE_ID_REQUIRED")
        return urllib.parse.quote(file_id, safe="")

    def _assert_only_visible_file(self, file_id: str) -> None:
        query = urllib.parse.urlencode({
            "q": self._quoted("trashed=false"),
            "spaces": "drive",
            "corpora": "user",
            "pageSize": "2",
            "fields": "files(id),nextPageToken",
        })
        result = self._request(
            f"{self.FILES_BASE}?{query}",
            method="GET",
            expect_json=True,
        )
        if "nextPageToken" in result:
            raise WorkbenchConflict("HOLD_VISIBLE_CORPUS_NOT_EXACTLY_ONE_TARGET")
        files = result.get("files")
        if not isinstance(files, list) or len(files) != 1:
            raise WorkbenchConflict("HOLD_VISIBLE_CORPUS_NOT_EXACTLY_ONE_TARGET")
        sole = files[0]
        if not isinstance(sole, dict):
            raise WorkbenchConflict("HOLD_VISIBLE_CORPUS_NOT_EXACTLY_ONE_TARGET")
        if sole.get("id") != file_id:
            raise WorkbenchConflict("HOLD_VISIBLE_CORPUS_NOT_EXACTLY_ONE_TARGET")

    def metadata(self, file_id: str) -> dict:
        quoted = self._quoted(file_id)
        result = self._request(
            f"{self.FILES_BASE}/{quoted}?fields=id,version,mimeType,modifiedTime",
            expect_json=True,
        )
        assert isinstance(result, dict)
        if result.get("id") != file_id:
            raise WorkbenchConflict("WORKBENCH_TARGET_ID_MISMATCH")
        if result.get("mimeType") != self.XLSX_MIME:
            raise WorkbenchConflict("WORKBENCH_TARGET_MIME_MISMATCH")
        return result

    def download(self, file_id: str) -> bytes:
        quoted = self._quoted(file_id)
        result = self._request(
            f"{self.FILES_BASE}/{quoted}?alt=media",
            expect_json=False,
        )
        if not isinstance(result, bytes):
            raise WorkbenchCapabilityDebt("GOOGLE_DRIVE_RESPONSE_INVALID")
        return result

    def replace(self, file_id: str, payload: bytes, mime_type: str) -> dict:
        if mime_type != self.XLSX_MIME:
            raise WorkbenchConflict("WORKBENCH_TARGET_MIME_MISMATCH")
        self._assert_only_visible_file(file_id)
        quoted = self._quoted(file_id)
        result = self._request(
            f"{self.UPLOAD_BASE}/{quoted}?uploadType=media&fields=id,version,mimeType,modifiedTime",
            method="PATCH",
            payload=payload,
            content_type=mime_type,
            expect_json=True,
        )
        assert isinstance(result, dict)
        if result.get("id") != file_id:
            raise WorkbenchConflict("WORKBENCH_TARGET_ID_MISMATCH")
        return result


class WorkbenchClaimHttpTransport:
    """HTTP client for the admitted Cloudflare/D1 workbench claim control paths."""

    def __init__(
        self,
        *,
        base_url: str,
        write_secret: str,
        timeout: float = 15,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        if not base_url.strip() or not write_secret:
            raise WorkbenchCapabilityDebt("WORKBENCH_CLAIM_BINDING_INCOMPLETE")
        self.base_url = base_url.rstrip("/")
        self.write_secret = write_secret
        self.timeout = timeout
        self._opener = opener

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            method="POST",
            headers={
                "authorization": f"Bearer {self.write_secret}",
                "content-type": "application/json",
                "accept": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            blocker = None
            try:
                parsed = json.loads(exc.read().decode("utf-8"))
                if isinstance(parsed, dict):
                    blocker = parsed.get("blocker") or parsed.get("error")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            suffix = blocker if isinstance(blocker, str) and blocker.strip() else f"HTTP_{exc.code}"
            raise WorkbenchCapabilityDebt(f"WORKBENCH_CLAIM_{suffix}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchCapabilityDebt("WORKBENCH_CLAIM_RESPONSE_INVALID") from exc
        if not isinstance(result, dict) or not isinstance(result.get("state"), str):
            raise WorkbenchCapabilityDebt("WORKBENCH_CLAIM_RESPONSE_INVALID")
        return result

    def claim(self, payload: dict) -> dict:
        return self._post("/internal/control/workbench-write-claim", payload)

    def complete(self, payload: dict) -> dict:
        return self._post("/internal/control/workbench-write-complete", payload)

    def fail(self, payload: dict) -> dict:
        return self._post("/internal/control/workbench-write-fail", payload)


class DriveXlsxWorkbenchPort:
    MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __init__(self, *, file_id: str, drive: DriveTransport, claims: ClaimTransport):
        if not file_id.strip():
            raise ValueError("WORKBENCH_FILE_ID_REQUIRED")
        self.file_id = file_id
        self.drive = drive
        self.claims = claims

    def write(
        self,
        *,
        task_id: str,
        intent_sha256: str,
        new_bytes: bytes,
    ) -> WorkbenchWriteReceipt:
        """Compatibility path for the isolated qualification command."""
        return self.mutate(
            task_id=task_id,
            intent_sha256=intent_sha256,
            build_new_bytes=lambda _preimage: new_bytes,
        )

    def mutate(
        self,
        *,
        task_id: str,
        intent_sha256: str,
        build_new_bytes: Callable[[bytes], bytes],
        verify_mutation: Callable[[bytes, bytes], None] | None = None,
        expected_preimage_version: str | None = None,
        expected_preimage_sha256: str | None = None,
    ) -> WorkbenchWriteReceipt:
        """Build against this transaction's exact preimage, then claim and read back."""
        meta = self.drive.metadata(self.file_id)
        pre_version = str(meta.get("version") or "")
        if not pre_version:
            raise WorkbenchCapabilityDebt("DRIVE_VERSION_UNAVAILABLE")
        pre_bytes = self.drive.download(self.file_id)
        pre_sha = sha256_hex(pre_bytes)
        if (
            (expected_preimage_version is not None and pre_version != expected_preimage_version)
            or (expected_preimage_sha256 is not None and pre_sha != expected_preimage_sha256)
        ):
            raise WorkbenchConflict("HOLD_STALE_PREIMAGE")
        new_bytes = build_new_bytes(pre_bytes)
        if not isinstance(new_bytes, (bytes, bytearray)):
            raise WorkbenchCapabilityDebt("WORKBENCH_MUTATION_BUILDER_INVALID_OUTPUT")
        new_bytes = bytes(new_bytes)
        if verify_mutation is not None:
            verify_mutation(pre_bytes, new_bytes)
        if pre_bytes == new_bytes:
            return WorkbenchWriteReceipt("NO_DELTA", self.file_id, pre_version, pre_sha)

        claim_id = hashlib.sha256(
            f"{self.file_id}:{pre_version}:{pre_sha}:{intent_sha256}:{task_id}".encode()
        ).hexdigest()
        claim = self.claims.claim(
            {
                "claim_id": claim_id,
                "file_id": self.file_id,
                "preimage_version": pre_version,
                "preimage_sha256": pre_sha,
                "intent_sha256": intent_sha256,
                "task_id": task_id,
            }
        )
        if claim.get("state") == "IDEMPOTENT_SUCCESS":
            claimed_post_sha = claim.get("postwrite_sha256")
            claimed_post_version = str(claim.get("postwrite_version") or "")
            current_meta = self.drive.metadata(self.file_id)
            current_bytes = self.drive.download(self.file_id)
            if (
                not claimed_post_version
                or not isinstance(claimed_post_sha, str)
                or str(current_meta.get("version") or "") != claimed_post_version
                or sha256_hex(current_bytes) != claimed_post_sha
                or claimed_post_sha != sha256_hex(new_bytes)
            ):
                raise WorkbenchConflict("HOLD_IDEMPOTENT_READBACK_MISMATCH")
            return WorkbenchWriteReceipt(
                "WRITE_AND_READBACK_PASS",
                self.file_id,
                pre_version,
                pre_sha,
                claimed_post_version,
                claimed_post_sha,
                claim_id,
            )
        if claim.get("state") != "CLAIMED":
            raise WorkbenchConflict(claim.get("blocker") or "WORKBENCH_WRITE_CLAIM_REJECTED")

        fresh_meta = self.drive.metadata(self.file_id)
        fresh_version = str(fresh_meta.get("version") or "")
        fresh_bytes = self.drive.download(self.file_id)
        if fresh_version != pre_version or sha256_hex(fresh_bytes) != pre_sha:
            self.claims.fail({"claim_id": claim_id, "blocker": "HOLD_STALE_PREIMAGE"})
            raise WorkbenchConflict("HOLD_STALE_PREIMAGE")

        expected_post_sha = sha256_hex(new_bytes)
        self.drive.replace(self.file_id, new_bytes, self.MIME)
        post_meta = self.drive.metadata(self.file_id)
        post_version = str(post_meta.get("version") or "")
        post_bytes = self.drive.download(self.file_id)
        post_sha = sha256_hex(post_bytes)
        if not post_version or post_version == pre_version or post_sha != expected_post_sha:
            self.claims.fail({"claim_id": claim_id, "blocker": "HOLD_POSTWRITE_MISMATCH"})
            raise WorkbenchPostwriteMismatch("HOLD_POSTWRITE_MISMATCH")

        complete = self.claims.complete(
            {
                "claim_id": claim_id,
                "postwrite_version": post_version,
                "postwrite_sha256": post_sha,
                "result_state": "WRITE_AND_READBACK_PASS",
            }
        )
        if complete.get("state") not in {"COMPLETED", "IDEMPOTENT_SUCCESS"}:
            raise WorkbenchCapabilityDebt("WORKBENCH_CLAIM_COMPLETION_FAILED")
        return WorkbenchWriteReceipt(
            "WRITE_AND_READBACK_PASS",
            self.file_id,
            pre_version,
            pre_sha,
            post_version,
            post_sha,
            claim_id,
    )
