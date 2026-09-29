from __future__ import annotations

import hashlib
import io
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from global_hybrid_v2.adapters.drive_xlsx_workbench import (
    DRIVE_WORKBENCH_SCOPE,
    DriveXlsxWorkbenchPort,
    GoogleDriveRestTransport,
    WorkbenchCapabilityDebt,
    WorkbenchClaimHttpTransport,
    WorkbenchConflict,
    sha256_hex,
)
from global_hybrid_v2.google_auth import ServiceAccountAccessTokenProvider, ServiceAccountIdentity
from global_hybrid_v2.runtime.deployment import RuntimeIdentity, read_runtime_identity

REQUIRED_SHEETS = (
    "摘要",
    "AI工作主表",
    "原始來源",
    "欄位契約",
    "VEHICLE_WORK_HISTORY",
    "MARKET_OBSERVATION_LEDGER",
    "OUTCOME_LINK_LEDGER",
    "CONFIG_COVERAGE",
    "SOURCE_SYNC_HISTORY",
)


@dataclass(frozen=True)
class QualificationBinding:
    file_id: str
    google_service_account_json: str
    control_base_url: str
    control_write_secret: str
    expected_commit: str
    expected_branch: str
    expected_repo_slug: str


@dataclass(frozen=True)
class QualificationReceipt:
    state: str
    file_id: str
    baseline_sha256: str
    probe_sha256: str
    restored_sha256: str
    probe_claim_id: str | None
    restore_claim_id: str | None
    sheets: tuple[str, ...]


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchCapabilityDebt(f"{key}_REQUIRED")
    return value


def load_binding(
    environ: Mapping[str, str] | None = None,
    runtime_identity: RuntimeIdentity | None = None,
) -> QualificationBinding:
    env = environ if environ is not None else os.environ
    if env.get("GLOBAL_WORKBENCH_QUALIFICATION_ENABLED", "").lower() != "true":
        raise WorkbenchCapabilityDebt("WORKBENCH_QUALIFICATION_DISABLED")
    if env.get("GLOBAL_LIVE_EXECUTION", "").lower() == "true":
        raise WorkbenchConflict("QUALIFICATION_REQUIRES_LIVE_EXECUTION_FALSE")

    runtime = runtime_identity or read_runtime_identity(env)
    expected_commit = _required(env, "GLOBAL_WORKBENCH_QUALIFICATION_EXPECTED_COMMIT")
    expected_branch = _required(env, "GLOBAL_WORKBENCH_QUALIFICATION_EXPECTED_BRANCH")
    expected_repo_slug = _required(env, "GLOBAL_WORKBENCH_QUALIFICATION_EXPECTED_REPO_SLUG")
    if runtime.provider != "RENDER":
        raise WorkbenchConflict("QUALIFICATION_RENDER_RUNTIME_REQUIRED")
    if runtime.git_commit != expected_commit:
        raise WorkbenchConflict("QUALIFICATION_COMMIT_MISMATCH")
    if runtime.git_branch != expected_branch:
        raise WorkbenchConflict("QUALIFICATION_BRANCH_MISMATCH")
    if runtime.repo_slug != expected_repo_slug:
        raise WorkbenchConflict("QUALIFICATION_REPO_MISMATCH")

    return QualificationBinding(
        file_id=_required(env, "GLOBAL_WORKBENCH_DRIVE_FILE_ID"),
        google_service_account_json=_required(env, "GLOBAL_GOOGLE_SERVICE_ACCOUNT_JSON"),
        control_base_url=_required(env, "GLOBAL_VEHICLE_CONTROL_HTTP_BASE_URL"),
        control_write_secret=_required(env, "GLOBAL_VEHICLE_CONTROL_HTTP_WRITE_SECRET"),
        expected_commit=expected_commit,
        expected_branch=expected_branch,
        expected_repo_slug=expected_repo_slug,
    )


def _sheet_names(payload: bytes) -> tuple[str, ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            workbook_xml = archive.read("xl/workbook.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise WorkbenchConflict("WORKBENCH_XLSX_TOPOLOGY_INVALID") from exc
    root = ET.fromstring(workbook_xml)
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return tuple(node.attrib["name"] for node in root.findall("main:sheets/main:sheet", ns))


def _probe_bytes(payload: bytes) -> bytes:
    baseline_sheets = _sheet_names(payload)
    if baseline_sheets != REQUIRED_SHEETS:
        raise WorkbenchConflict("WORKBENCH_XLSX_TOPOLOGY_MISMATCH")
    stream = io.BytesIO(payload)
    marker = f"RD021:{sha256_hex(payload)[:24]}".encode()
    with zipfile.ZipFile(stream, "a") as archive:
        if archive.comment == marker:
            marker += b":probe"
        archive.comment = marker
    result = stream.getvalue()
    if result == payload:
        raise WorkbenchCapabilityDebt("WORKBENCH_PROBE_MUTATION_UNAVAILABLE")
    if _sheet_names(result) != baseline_sheets:
        raise WorkbenchConflict("WORKBENCH_XLSX_TOPOLOGY_MISMATCH")
    return result


def build_port(binding: QualificationBinding) -> DriveXlsxWorkbenchPort:
    token_provider = ServiceAccountAccessTokenProvider(
        ServiceAccountIdentity.from_json(binding.google_service_account_json),
        scopes=(DRIVE_WORKBENCH_SCOPE,),
    )
    drive = GoogleDriveRestTransport(token_provider)
    claims = WorkbenchClaimHttpTransport(
        base_url=binding.control_base_url,
        write_secret=binding.control_write_secret,
    )
    return DriveXlsxWorkbenchPort(file_id=binding.file_id, drive=drive, claims=claims)


def run_qualification(
    binding: QualificationBinding,
    *,
    port: DriveXlsxWorkbenchPort | None = None,
) -> QualificationReceipt:
    workbench = port or build_port(binding)
    baseline = workbench.drive.download(binding.file_id)
    baseline_sha = sha256_hex(baseline)
    sheets = _sheet_names(baseline)
    if sheets != REQUIRED_SHEETS:
        raise WorkbenchConflict("WORKBENCH_XLSX_TOPOLOGY_MISMATCH")
    probe = _probe_bytes(baseline)
    probe_sha = sha256_hex(probe)

    probe_receipt = workbench.write(
        task_id=f"rd021-qualify-probe-{baseline_sha[:16]}",
        intent_sha256=hashlib.sha256(f"probe:{baseline_sha}:{probe_sha}".encode()).hexdigest(),
        new_bytes=probe,
    )
    if probe_receipt.state != "WRITE_AND_READBACK_PASS":
        raise WorkbenchConflict("WORKBENCH_QUALIFICATION_PROBE_NOT_WRITTEN")

    try:
        restore_receipt = workbench.write(
            task_id=f"rd021-qualify-restore-{probe_sha[:16]}",
            intent_sha256=hashlib.sha256(f"restore:{probe_sha}:{baseline_sha}".encode()).hexdigest(),
            new_bytes=baseline,
        )
    except Exception as exc:
        raise WorkbenchCapabilityDebt("WORKBENCH_QUALIFICATION_RESTORE_FAILED") from exc
    if restore_receipt.state != "WRITE_AND_READBACK_PASS":
        raise WorkbenchCapabilityDebt("WORKBENCH_QUALIFICATION_RESTORE_FAILED")

    restored = workbench.drive.download(binding.file_id)
    restored_sha = sha256_hex(restored)
    if restored_sha != baseline_sha or restored != baseline:
        raise WorkbenchCapabilityDebt("WORKBENCH_QUALIFICATION_FINAL_READBACK_MISMATCH")
    if _sheet_names(restored) != sheets:
        raise WorkbenchCapabilityDebt("WORKBENCH_QUALIFICATION_FINAL_TOPOLOGY_MISMATCH")

    return QualificationReceipt(
        state="QUALIFICATION_PASS",
        file_id=binding.file_id,
        baseline_sha256=baseline_sha,
        probe_sha256=probe_sha,
        restored_sha256=restored_sha,
        probe_claim_id=probe_receipt.claim_id,
        restore_claim_id=restore_receipt.claim_id,
        sheets=sheets,
    )


def main() -> None:
    receipt = run_qualification(load_binding())
    print(json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
