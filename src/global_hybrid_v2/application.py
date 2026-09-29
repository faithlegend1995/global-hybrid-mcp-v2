from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from global_hybrid_v2.adapters.drive_xlsx_workbench import (
    DRIVE_WORKBENCH_SCOPE,
    DriveXlsxWorkbenchPort,
    GoogleDriveRestTransport,
    WorkbenchClaimHttpTransport,
)
from global_hybrid_v2.adapters.file_vehicle_configuration import (
    configured_vehicle_configuration_provider,
)
from global_hybrid_v2.adapters.http_vehicle_configuration import (
    HttpVehicleConfigurationProvider,
    UrlLibVehicleProviderTransport,
)
from global_hybrid_v2.adapters.openai_research import configured_research_port
from global_hybrid_v2.canonical_completion import (
    CanonicalCompanyCommercialCompletionHandler,
    ServerVerifiedMutationProvider,
)
from global_hybrid_v2.company_commercial_completion import (
    CANONICAL_WORKBENCH_FILE_ID,
    CompanyCommercialCompletionHandler,
    CompanyCommercialCompletionPort,
)
from global_hybrid_v2.contracts import Owner
from global_hybrid_v2.domains.base import DomainPort
from global_hybrid_v2.domains.library_projection import LibraryProjectionDomain
from global_hybrid_v2.domains.sales_human import SalesHumanDomain
from global_hybrid_v2.domains.stubs import NotConfiguredDomain
from global_hybrid_v2.domains.vehicle_configuration import VehicleConfigurationProvider
from global_hybrid_v2.google_auth import ServiceAccountAccessTokenProvider, ServiceAccountIdentity
from global_hybrid_v2.governance.authority import AuthorityResolver
from global_hybrid_v2.governance.fitness import FitnessReport, SystemFitnessFunctions
from global_hybrid_v2.governance.host_projection import HostCurrentStateVerifier, HostProjectionGate
from global_hybrid_v2.ingress_admission import IngressTurnTokenCodec
from global_hybrid_v2.inventory_runtime_binding import InventoryRuntime
from global_hybrid_v2.observer.witness import ReadOnlyWitness
from global_hybrid_v2.research import (
    ResearchExecutor,
    ResearchPort,
)
from global_hybrid_v2.runtime.deployment import RuntimeIdentity, read_runtime_identity
from global_hybrid_v2.runtime.dispatcher import Dispatcher
from global_hybrid_v2.runtime.trace import TraceBus
from global_hybrid_v2.settings import Settings
from global_hybrid_v2.transactional_vehicle_store import EvidenceAdmissionPort, TransactionalVehicleStore
from global_hybrid_v2.trusted_workbench_intent import TrustedHostTaskCompiler
from global_hybrid_v2.workbench_mutation import XlsxWorkbenchMutationBuilder


@dataclass(frozen=True)
class Application:
    repo_root: Path
    settings: Settings
    authority: AuthorityResolver
    research_executor: ResearchExecutor
    runtime_identity: RuntimeIdentity
    trace: TraceBus
    dispatcher: Dispatcher
    vehicle_configuration_provider: VehicleConfigurationProvider | None = None
    composition_fitness: FitnessReport | None = None
    trusted_host_task_compiler: TrustedHostTaskCompiler | None = None
    ingress_token_codec: IngressTurnTokenCodec | None = None
    consumer_binding_readback: Callable[[], dict] | None = None
    inventory_runtime: InventoryRuntime | None = None


def create_application(
    *,
    repo_root: str | Path | None = None,
    settings: Settings | None = None,
    trace: TraceBus | None = None,
    research: ResearchPort | None = None,
    runtime_identity: RuntimeIdentity | None = None,
    host_current_state_verifier: HostCurrentStateVerifier | None = None,
    vehicle_configuration_provider: VehicleConfigurationProvider | None = None,
    company_commercial_completion_handler: CompanyCommercialCompletionPort | None = None,
    canonical_verified_mutation_provider: ServerVerifiedMutationProvider | None = None,
    canonical_evidence_admission: EvidenceAdmissionPort | None = None,
    trusted_host_task_compiler: TrustedHostTaskCompiler | None = None,
    ingress_token_codec: IngressTurnTokenCodec | None = None,
) -> Application:
    root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    runtime_settings = settings or Settings()
    if runtime_settings.media_enabled:
        runtime_settings.require_media_deployment_bindings()
    effective_runtime_identity = runtime_identity or read_runtime_identity()
    registry_path = Path(runtime_settings.authority_registry)
    if not registry_path.is_absolute():
        registry_path = root / registry_path

    authority = AuthorityResolver(
        registry_path,
        trusted_key_id=runtime_settings.authority_trusted_key_id,
        trusted_public_key=runtime_settings.authority_trusted_public_key,
    )
    runtime_trace = trace or TraceBus()
    runtime_trace.attach_witness(ReadOnlyWitness())
    research_port = research if research is not None else configured_research_port(runtime_settings)
    research_executor = ResearchExecutor(research_port)
    effective_vehicle_configuration_provider = vehicle_configuration_provider
    if effective_vehicle_configuration_provider is None:
        if runtime_settings.vehicle_configuration_provider_mode == "cloudflare_d1_http":
            if (
                not runtime_settings.vehicle_configuration_http_base_url
                or not runtime_settings.vehicle_configuration_http_read_secret
            ):
                raise RuntimeError("cloudflare D1 HTTP vehicle provider is incompletely configured")
            effective_vehicle_configuration_provider = HttpVehicleConfigurationProvider(
                UrlLibVehicleProviderTransport(
                    base_url=runtime_settings.vehicle_configuration_http_base_url,
                    read_secret=runtime_settings.vehicle_configuration_http_read_secret,
                )
            )
        elif runtime_settings.vehicle_configuration_provider_mode == "file":
            effective_vehicle_configuration_provider = configured_vehicle_configuration_provider(
                runtime_settings,
                repo_root=root,
            )
        else:
            raise RuntimeError("unknown vehicle configuration provider mode")
    domains: dict[Owner, DomainPort] = {owner: NotConfiguredDomain(owner) for owner in Owner}
    domains[Owner.LIBRARY_FACT] = LibraryProjectionDomain(
        vehicle_configuration_provider=effective_vehicle_configuration_provider
    )
    domains[Owner.SALES_HUMAN] = SalesHumanDomain()
    composition_fitness = SystemFitnessFunctions.evaluate_composition(
        domains=domains,
        trace=runtime_trace,
    )
    if not composition_fitness.passed:
        blockers = ", ".join(
            check.blocker or check.name for check in composition_fitness.checks if not check.passed
        )
        raise RuntimeError(f"runtime composition fitness failed: {blockers}")
    completion_handler = company_commercial_completion_handler
    if runtime_settings.canonical_vehicle_store_mode == "postgres":
        if completion_handler is not None:
            raise RuntimeError("CANONICAL_MODE_FORBIDS_INJECTED_COMPLETION")
        if (runtime_settings.canonical_postgres_dsn is None
            or canonical_verified_mutation_provider is None
            or canonical_evidence_admission is None):
            raise RuntimeError("CANONICAL_STORE_BINDING_INCOMPLETE")
        completion_handler = CanonicalCompanyCommercialCompletionHandler(
            store=TransactionalVehicleStore.postgres_candidate(
                runtime_settings.canonical_postgres_dsn.get_secret_value(),
                evidence_admission=canonical_evidence_admission,
            ),
            verified_mutations=canonical_verified_mutation_provider,
        )
    elif completion_handler is None:
        credential = runtime_settings.google_service_account_json
        claim_secret = runtime_settings.vehicle_control_http_write_secret
        claim_url = runtime_settings.vehicle_control_http_base_url
        if credential is not None and claim_secret is not None and claim_url:
            identity = ServiceAccountIdentity.from_json(credential.get_secret_value())
            tokens = ServiceAccountAccessTokenProvider(
                identity, scopes=(DRIVE_WORKBENCH_SCOPE,),
            )
            completion_handler = CompanyCommercialCompletionHandler(
                writer=DriveXlsxWorkbenchPort(
                    file_id=CANONICAL_WORKBENCH_FILE_ID,
                    drive=GoogleDriveRestTransport(tokens),
                    claims=WorkbenchClaimHttpTransport(
                        base_url=claim_url, write_secret=claim_secret.get_secret_value(),
                    ),
                ),
                builder=XlsxWorkbenchMutationBuilder(),
            )
    dispatcher = Dispatcher(
        authority=authority,
        domains=domains,
        trace=runtime_trace,
        research_executor=research_executor,
        runtime_commit=effective_runtime_identity.git_commit,
        runtime_branch=effective_runtime_identity.git_branch,
        host_projection_gate=HostProjectionGate(verifier=host_current_state_verifier),
        company_commercial_completion_handler=completion_handler,
    )
    return Application(
        repo_root=root,
        settings=runtime_settings,
        authority=authority,
        research_executor=research_executor,
        runtime_identity=effective_runtime_identity,
        trace=runtime_trace,
        dispatcher=dispatcher,
        vehicle_configuration_provider=effective_vehicle_configuration_provider,
        composition_fitness=composition_fitness,
        trusted_host_task_compiler=trusted_host_task_compiler,
        ingress_token_codec=ingress_token_codec,
    )
