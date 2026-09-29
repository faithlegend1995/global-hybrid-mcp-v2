import pytest

from global_hybrid_v2.adapters.drive_xlsx_workbench import (
    DRIVE_WORKBENCH_SCOPE,
)
from global_hybrid_v2.google_auth import (
    GoogleAuthUnavailable,
    ServiceAccountAccessTokenProvider,
)


def test_default_scope_remains_spreadsheets_only():
    provider = ServiceAccountAccessTokenProvider(object())
    assert provider.scopes == ("https://www.googleapis.com/auth/spreadsheets",)
    assert DRIVE_WORKBENCH_SCOPE not in provider.scopes
    assert all("drive.file" not in scope for scope in provider.scopes)


def test_explicit_scope_is_preserved_without_widening():
    provider = ServiceAccountAccessTokenProvider(object(), scopes=("scope:one", "scope:two"))
    assert provider.scopes == ("scope:one", "scope:two")


@pytest.mark.parametrize("scopes", [(), ("",), ("   ",)])
def test_empty_or_blank_scope_fails_closed(scopes):
    with pytest.raises(GoogleAuthUnavailable, match="explicit and non-blank"):
        ServiceAccountAccessTokenProvider(object(), scopes=scopes)
