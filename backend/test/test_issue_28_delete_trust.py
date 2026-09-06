from datetime import datetime, timedelta, timezone


def _payload(now):
    return {
        "schema_version": 1,
        "mode": "fenced_writers_iam_no_overwrite",
        "result": "valid",
        "environment_id": "prod-cn-shanghai-primary",
        "formal_bucket": "formal-images-private",
        "iam_policy_sha256": "a" * 64,
        "writer_inventory_sha256": "b" * 64,
        "verified_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=12)).isoformat(),
    }


def test_no_overwrite_attestation_allows_only_exact_environment_and_bucket():
    from services.purge_delete_trust import NoOverwriteTrustAttestation

    now = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
    attestation = NoOverwriteTrustAttestation.from_dict(_payload(now))

    decision = attestation.evaluate(
        now=now + timedelta(minutes=1),
        environment_id="prod-cn-shanghai-primary",
        formal_bucket="formal-images-private",
    )

    assert decision.allowed is True
    assert decision.error_code is None
    assert len(decision.attestation_sha256) == 64


def test_no_overwrite_attestation_fails_closed_for_scope_age_or_mode_change():
    from services.purge_delete_trust import NoOverwriteTrustAttestation

    now = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
    attestation = NoOverwriteTrustAttestation.from_dict(_payload(now))
    assert attestation.evaluate(
        now=now + timedelta(minutes=1),
        environment_id="staging",
        formal_bucket="formal-images-private",
    ).allowed is False
    assert attestation.evaluate(
        now=now + timedelta(days=1),
        environment_id="prod-cn-shanghai-primary",
        formal_bucket="formal-images-private",
    ).allowed is False

    changed = _payload(now)
    changed["mode"] = "exact_version"
    try:
        NoOverwriteTrustAttestation.from_dict(changed)
    except ValueError as exc:
        assert "mode" in str(exc)
    else:
        raise AssertionError("unsupported trust mode must fail closed")
