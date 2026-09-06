"""#27 binding/purge fence production composition shared by all ingest factories.

Enablement is explicit (``INGEST_BINDING_FENCE_ENABLED``) so each production entry
point adopts the durable binding fence as an auditable deployment step; when
disabled the factories behave exactly as before (no fence kwargs).
"""

from __future__ import annotations

import os
import hashlib
import json
from contextlib import contextmanager

from sqlalchemy.orm import sessionmaker

from services.object_binding_fence import ObjectBindingFenceService
from services.purge_object_fence import PurgeObjectFenceService

_TRUE_VALUES = ('1', 'true', 'yes', 'on')
FORMAL_OBJECT_WRITER_INVENTORY = (
    'http:image_assets.import',
    'http:image_imports.queue',
    'http:products.create_update',
    'operator:kodo_migration',
    'worker:image_import_promotion',
    'worker:import_cleanup',
)


def formal_writer_inventory_sha256() -> str:
    canonical = json.dumps(
        FORMAL_OBJECT_WRITER_INVENTORY,
        ensure_ascii=True,
        separators=(',', ':'),
    ).encode('ascii')
    return hashlib.sha256(canonical).hexdigest()


def fence_capability_enabled(values) -> bool:
    raw = values.get('INGEST_BINDING_FENCE_ENABLED', '0')
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE_VALUES


def validate_formal_writer_deployment(values) -> None:
    deployed = str(
        values.get('PURGE_FORMAL_DELETION_DEPLOYED', '0')
    ).strip().lower() in _TRUE_VALUES
    if deployed and not fence_capability_enabled(values):
        raise ValueError('formal deployment requires binding fence on every writer')


def binding_fence_kwargs(values) -> dict:
    """返回入库服务构造围栏 kwargs；未启用时返回空 dict（行为与今日一致）。

    启用但没有正式 Bucket 是部署错误：立即抛错，不静默降级成无围栏写入。
    围栏自带 session 在 control-factory 模式下不被使用，但仍注入 db.session
    以兼容未启用/回退路径，组合方式与 import worker/cleanup 一致。
    """
    validate_formal_writer_deployment(values)
    fence_enabled = fence_capability_enabled(values)
    if not fence_enabled:
        return {}
    formal_bucket = str(values.get('OSS_BUCKET_NAME') or '').strip()
    if not formal_bucket:
        raise ValueError('启用绑定围栏要求配置 OSS_BUCKET_NAME')
    from models import db

    def control_session_factory():
        return sessionmaker(bind=db.engine)()

    return {
        'formal_bucket': formal_bucket,
        'binding_fence_service': ObjectBindingFenceService(
            db.session,
            purge_fence_service=PurgeObjectFenceService(db.session),
        ),
        'control_session_factory': control_session_factory,
    }


def request_fence_kwargs():
    """Flask 请求内组合：config 覆盖进程环境。"""
    from flask import current_app

    class _Values:
        def get(self, key, default=None):
            if key in current_app.config:
                return current_app.config[key]
            return os.environ.get(key, default)

    return binding_fence_kwargs(_Values())


@contextmanager
def caller_owned_ingest_boundary(ingest_service):
    """caller-owned 多图事务全有或全无边界。

    yield 出一个 leases 列表；调用方把每个 ``result.binding_lease`` 追加进来。
    失败时先回滚调用方事务（释放 finalize 可能持有的围栏行锁），再经独立
    control session 逐个释放租约。未注入围栏的旧配置下 leases 恒空、abort
    恒 inert，边界对 legacy 路径无副作用。
    """
    leases: list = []
    try:
        yield leases
    except Exception as exc:
        from models import db

        db.session.rollback()
        # finalize 已开始后服务抛出的失败把租约挂在异常上（服务不能替调用方
        # 释放仍被其事务持锁的围栏）；回滚后行锁已释放，边界在此统一回收。
        attached = getattr(exc, 'binding_fence_lease', None)
        if attached is not None:
            leases.append(attached)
        abort = getattr(ingest_service, 'abort_after_outer_rollback', None)
        if abort is not None:
            for lease in leases:
                abort(lease)
        raise
