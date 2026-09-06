"""T14 formal-purge one-shot composition root, hard-disabled by default.

This root is deliberately separate from the #26 backup worker.  Until the
batch-bound capability and authorized deleter adapters are explicitly composed,
``main`` exits before creating a database repository or object-storage client.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from services.fence_composition import (
    formal_writer_inventory_sha256,
    validate_formal_writer_deployment,
)
from services.purge_formal_deletion_capability import (
    UnavailableFormalDeletionCapabilitySource,
)
from services.formal_purge_observability import (
    FormalPurgeOperationalEvent,
    JsonFormalPurgeEventSink,
    write_formal_purge_health,
)


EXIT_COMPLETE = 0
EXIT_DISABLED = 2
EXIT_FAILED = 3


def run_one_shot(
    *,
    worker_factory,
    capability,
    context=None,
    health_path=None,
    event_sink=None,
    now=None,
) -> int:
    """Evaluate the hard gate before constructing any privileged dependency."""
    moment = now or datetime.now(timezone.utc)
    environment_id = getattr(context, 'environment_id', 'unconfigured')
    batch_id = getattr(context, 'batch_id', uuid.UUID(int=0))

    def publish(result, stage, error_code, event_type):
        if health_path is not None:
            write_formal_purge_health(
                Path(health_path), now=moment, result=result,
                environment_id=environment_id, batch_id=batch_id,
                stage=stage, error_code=error_code,
            )
        if event_sink is not None:
            event_sink.emit(FormalPurgeOperationalEvent(
                event_type=event_type, occurred_at=moment,
                environment_id=environment_id, batch_id=batch_id,
                target_asset_id=None, checkpoint=None,
                result=(
                    'disabled' if result == 'disabled'
                    else 'failed' if result == 'failed'
                    else 'succeeded'
                ),
                error_code=error_code,
            ))

    try:
        validate_formal_writer_deployment(os.environ)
        digest = getattr(context, 'writer_inventory_sha256', None)
        if (
            digest is not None
            and digest != formal_writer_inventory_sha256()
        ):
            raise ValueError('formal worker inventory digest mismatch')
    except Exception:
        publish(
            'disabled', 'preflight', 'PURGE_FORMAL_DELETION_DISABLED',
            'purge.formal.capability.denied',
        )
        return EXIT_DISABLED
    try:
        if not capability.evaluate(context):
            publish(
                'disabled', 'capability', 'PURGE_FORMAL_DELETION_DISABLED',
                'purge.formal.capability.denied',
            )
            return EXIT_DISABLED
    except Exception:
        publish(
            'failed', 'capability', 'PURGE_FORMAL_CAPABILITY_INVALID',
            'purge.formal.capability.failed',
        )
        return EXIT_DISABLED
    try:
        worker = worker_factory()
        worker.process_one_item()
        publish('valid', 'complete', None, 'purge.formal.one_shot.completed')
        return EXIT_COMPLETE
    except Exception:
        publish(
            'failed', 'worker', 'PURGE_FORMAL_WORKER_FAILED',
            'purge.formal.one_shot.failed',
        )
        return EXIT_FAILED


def _unavailable_worker_factory():
    raise RuntimeError("formal purge production composition is unavailable")


def main() -> int:
    health_value = os.getenv('PURGE_FORMAL_HEALTH_PATH')
    context = SimpleNamespace(
        environment_id=os.getenv('PURGE_FORMAL_ENVIRONMENT_ID', 'unconfigured'),
        batch_id=uuid.UUID(
            os.getenv('PURGE_FORMAL_BATCH_ID', str(uuid.UUID(int=0)))
        ),
    )
    logger = logging.getLogger('formal_purge_worker')
    return run_one_shot(
        worker_factory=_unavailable_worker_factory,
        capability=UnavailableFormalDeletionCapabilitySource(),
        context=context,
        health_path=Path(health_value) if health_value else None,
        event_sink=JsonFormalPurgeEventSink(logger.info),
    )


if __name__ == "__main__":
    raise SystemExit(main())
