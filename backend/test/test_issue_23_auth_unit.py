import hashlib
import hmac
from unittest import mock

import pytest

from services.admin_auth import AdminAuth, AdminAuthError, AdminPrincipal


def test_unconfigured_none_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth(None).authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_empty_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("").authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_whitespace_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("   ").authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_and_wrong_token_both_call_compare_digest():
    with mock.patch("services.admin_auth.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
        with pytest.raises(AdminAuthError) as unconfigured:
            AdminAuth(None).authenticate("Bearer secret")
        with pytest.raises(AdminAuthError) as wrong:
            AdminAuth("correct-token").authenticate("Bearer wrong-token")
    assert unconfigured.value.error_code == "AUTH_NOT_CONFIGURED"
    assert wrong.value.error_code == "AUTH_FORBIDDEN"
    assert compare.call_count == 2
    for args, _kwargs in compare.call_args_list:
        assert len(args[0]) == hashlib.sha256().digest_size
        assert len(args[1]) == hashlib.sha256().digest_size


def test_missing_header_required():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("correct-token").authenticate(None)
    assert exc.value.error_code == "AUTH_REQUIRED"


def test_malformed_and_basic_scheme_required():
    auth = AdminAuth("correct-token")
    for header in ("Bearer", "Bearer ", "Basic abc", "Token abc"):
        with pytest.raises(AdminAuthError) as exc:
            auth.authenticate(header)
        assert exc.value.error_code == "AUTH_REQUIRED"


def test_bearer_scheme_is_case_insensitive():
    principal = AdminAuth("correct-token", actor_id="ops").authenticate(
        "bEaReR correct-token"
    )
    assert principal == AdminPrincipal(actor_id="ops", role="admin")


def test_wrong_token_forbidden_and_correct_token_allows():
    auth = AdminAuth("  correct-token  ")
    with pytest.raises(AdminAuthError) as exc:
        auth.authenticate("Bearer other")
    assert exc.value.error_code == "AUTH_FORBIDDEN"
    assert auth.authenticate("Bearer correct-token").role == "admin"


def test_error_does_not_contain_token():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("super-secret-value").authenticate("Bearer super-secret-valueX")
    dumped = f"{exc.value} {exc.value.message} {exc.value.error_code}"
    assert "super-secret-value" not in dumped
