"""运行独立、可重启的持久图片导入 worker。"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from app import create_app
from models import db
from services.embedding import EmbeddingClient
from services.image_import_worker import (
    ImageImportWorker,
    SqlAlchemyImageImportRepository,
)
from services.object_storage import OssObjectStorage


logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(_signum, _frame):
    global _stop_requested
    _stop_requested = True


def main():
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    worker_id = os.getenv(
        'IMAGE_IMPORT_WORKER_ID',
        f'{socket.gethostname()}-{os.getpid()}',
    )
    lease_seconds = int(os.getenv('IMAGE_IMPORT_LEASE_SECONDS', '300'))
    poll_seconds = float(os.getenv('IMAGE_IMPORT_POLL_SECONDS', '2'))

    app = create_app()
    with app.app_context():
        worker = ImageImportWorker(
            repository=SqlAlchemyImageImportRepository(db.session),
            storage=OssObjectStorage.from_env(),
            embedding_client=EmbeddingClient(),
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        logger.info('image_import.worker_started worker_id=%s', worker_id)
        while not _stop_requested:
            if not worker.process_one():
                time.sleep(poll_seconds)
        logger.info('image_import.worker_stopped worker_id=%s', worker_id)


if __name__ == '__main__':
    main()

