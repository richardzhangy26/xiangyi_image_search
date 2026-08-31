"""运行独立的导入暂存对象清理任务。

安全边界：
- 默认不启用。必须显式设置 ``IMPORT_CLEANUP_ENABLED=true`` 才会执行清理；
  未启用时进程只记录日志并退出，便于部署编排而不触发任何对象删除。
- 只调用注入的对象存储删除适配；绝不连接 Kodo，绝不修改 ACL。
- SIGTERM/SIGINT 优雅停止；每项独立事务，可重启续跑。
"""

from __future__ import annotations

import logging
import os
import signal
import time

from app import create_app
from models import db
from services.import_cleanup import cleanup_expired_imports
from services.object_binding_fence import ObjectBindingFenceService
from services.object_storage import OssObjectStorage
from services.purge_object_fence import PurgeObjectFenceService


logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(_signum, _frame):
    global _stop_requested
    _stop_requested = True


def main():
    if os.getenv('IMPORT_CLEANUP_ENABLED', 'false').lower() != 'true':
        logger.info(
            'import_cleanup.disabled 需要 IMPORT_CLEANUP_ENABLED=true 才执行清理'
        )
        return

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    poll_seconds = float(os.getenv('IMPORT_CLEANUP_POLL_SECONDS', '300'))
    batch = int(os.getenv('IMPORT_CLEANUP_BATCH', '50'))

    app = create_app()
    with app.app_context():
        storage = OssObjectStorage.from_env()
        logger.info('import_cleanup.started poll_seconds=%s', poll_seconds)
        while not _stop_requested:
            try:
                processed = cleanup_expired_imports(
                    db.session,
                    storage=storage,
                    limit=batch,
                    binding_fence_service=ObjectBindingFenceService(
                        db.session,
                        purge_fence_service=PurgeObjectFenceService(db.session),
                    ),
                    formal_bucket=os.getenv('OSS_BUCKET_NAME'),
                )
                if processed:
                    logger.info(
                        'import_cleanup.cycle processed=%s', processed
                    )
            except Exception:
                logger.exception('import_cleanup.cycle_failed')
            for _ in range(int(poll_seconds)):
                if _stop_requested:
                    break
                time.sleep(1)
        logger.info('import_cleanup.stopped')


if __name__ == '__main__':
    main()
