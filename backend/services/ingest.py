"""图片入库：内容哈希去重 → 落盘 → 生成向量 → 写表。

CLI（scripts/ingest_images.py）与 HTTP 端点（blueprints/products_v2.py）共用本模块。
所有方法只 db.session.add，不 commit —— 事务边界由调用方掌握。
"""
import hashlib
import logging
import os
from dataclasses import dataclass

from models import ProductImage, db
from services.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

# IN 子句一次塞太多参数会拖慢查询，分块处理
_HASH_QUERY_CHUNK = 1000

# 文件名只取哈希前 16 个十六进制字符（64 bit）。唯一性由库里
# 完整 64 字符哈希的 UNIQUE 约束保证，截断只影响可读性。
_FILENAME_HASH_LEN = 16


@dataclass
class PendingImage:
    """CLI 扫描阶段产出的待入库项。"""
    model_number: str
    source_path: str
    content_hash: str
    image_order: int
    is_primary: bool


@dataclass
class IngestResult:
    model_number: str
    content_hash: str
    status: str                      # 'created' | 'duplicate' | 'failed'
    image_path: str = None
    duplicate_of: str = None
    error: str = None
    source_path: str = None
    fs_path: str = None              # created 时的文件系统绝对路径；调用方 rollback 需要它来清理孤儿文件


def hash_bytes(data):
    """源文件原始字节的 SHA-256（十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    """流式计算文件哈希，避免大图占内存。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_ext(filename):
    """取小写扩展名；未知扩展名回退为 .jpg。"""
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else '.jpg'


def storage_paths(upload_folder, model_number, content_hash, ext):
    """哈希命名，天然幂等：同一张图永远落在同一路径。

    返回 (web_path, filesystem_path)。
    """
    relative = f'product_images/{model_number}/{content_hash[:_FILENAME_HASH_LEN]}{ext}'
    return f'/uploads/{relative}', os.path.join(upload_folder, relative)


def find_existing_hashes(hashes):
    """返回 {content_hash: 已存在的 image_path}，只包含库里已有的。"""
    if not hashes:
        return {}

    unique = list({h for h in hashes if h})
    found = {}
    for start in range(0, len(unique), _HASH_QUERY_CHUNK):
        chunk = unique[start:start + _HASH_QUERY_CHUNK]
        rows = db.session.query(
            ProductImage.content_hash, ProductImage.image_path
        ).filter(ProductImage.content_hash.in_(chunk)).all()
        found.update({content_hash: image_path for content_hash, image_path in rows})
    return found


class ImageIngestService:
    def __init__(self, embedding_client=None):
        self._embedding = embedding_client or EmbeddingClient()

    def ingest_one(self, model_number, data, filename, upload_folder,
                   image_order=0, is_primary=False, request_id=None):
        """单张入库（HTTP 上传路径）。重复返回 duplicate，不抛异常。

        事务契约：本方法只 db.session.add，不 commit；成功路径会立即把文件写入磁盘。
        调用方若之后 rollback，DB 行会消失但磁盘文件不会自动清理——请用返回结果里
        status == 'created' 的 fs_path 字段自行删除该文件。

        并发契约：判重（find_existing_hashes）与写入之间没有加锁。两个进程/线程各自
        独立 session 并发上传同一张*新*图时，双方都会通过判重、都返回 'created'；
        最终唯一性由数据库 content_hash 的 UNIQUE 约束保证——先 commit 的一方成功，
        后 commit 的一方会在**调用方**的 db.session.commit() 处抛出 IntegrityError，
        调用方必须准备好接住它。因为文件名由哈希决定，双方写入的是同一路径、同一
        字节内容，覆盖是幂等的，不会发生内容错位。
        """
        content_hash = hash_bytes(data)

        existing = find_existing_hashes([content_hash])
        if content_hash in existing:
            logger.info(
                'ingest.duplicate model_number=%s content_hash=%s duplicate_of=%s',
                model_number, content_hash, existing[content_hash],
            )
            return IngestResult(
                model_number=model_number, content_hash=content_hash,
                status='duplicate', duplicate_of=existing[content_hash],
            )

        ext = normalized_ext(filename)
        web_path, fs_path = storage_paths(upload_folder, model_number, content_hash, ext)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)
        with open(fs_path, 'wb') as handle:
            handle.write(data)

        try:
            vector = self._embedding.embed_image(fs_path, request_id=request_id)
        except Exception:
            _remove_quietly(fs_path)   # 不留孤儿文件
            raise

        db.session.add(ProductImage(
            model_number=model_number,
            image_path=web_path,
            vector=vector.tolist(),
            content_hash=content_hash,
            original_path=fs_path,
            image_order=image_order,
            is_primary=is_primary,
        ))
        return IngestResult(
            model_number=model_number, content_hash=content_hash,
            status='created', image_path=web_path, fs_path=fs_path,
        )

    def ingest_pending(self, pending, upload_folder, request_id=None):
        """批量入库（CLI 路径）。调用方需保证 pending 长度 <= EmbeddingClient 的批大小。

        库内去重由调用方预先过滤；这里只处理批内重复。

        批内去重是乐观的：重复项在拿到 embedding 结果之前就先被标记为 duplicate，
        duplicate_of 指向同批次首现项*预期*会落在的路径。如果首现项的 embedding
        失败，它从未落盘、DB 里也没有对应行——这时所有依赖它的批内重复项会在
        embedding 结果回来后被回填为 failed（而不是停留在 duplicate），避免一张图
        被静默丢弃且没有报错信号。

        事务/并发契约与 ingest_one 一致：只 add 不 commit，成功项会立即落盘，
        rollback 需要调用方用 fs_path 自行清理；判重不加锁，最终一致性由 DB
        UNIQUE 约束保证。
        """
        if not pending:
            return []

        results = [None] * len(pending)
        # content_hash -> 批内首次出现该哈希的 pending 下标
        first_index_by_hash = {}
        # content_hash -> 依赖该首现项的批内重复项下标列表
        duplicate_indices_by_hash = {}
        to_embed = []          # [(下标, PendingImage)]，只含每个哈希的首现项

        for index, item in enumerate(pending):
            if item.content_hash in first_index_by_hash:
                duplicate_indices_by_hash.setdefault(item.content_hash, []).append(index)
                continue
            first_index_by_hash[item.content_hash] = index
            to_embed.append((index, item))

        if not to_embed:
            return results

        vectors = self._embedding.embed_images(
            [item.source_path for _, item in to_embed], request_id=request_id
        )

        for (index, item), vector in zip(to_embed, vectors):
            dup_indices = duplicate_indices_by_hash.get(item.content_hash, [])

            if vector is None:
                results[index] = IngestResult(
                    model_number=item.model_number, content_hash=item.content_hash,
                    status='failed', error='向量生成失败', source_path=item.source_path,
                )
                for dup_index in dup_indices:
                    dup_item = pending[dup_index]
                    results[dup_index] = IngestResult(
                        model_number=dup_item.model_number, content_hash=dup_item.content_hash,
                        status='failed', error='同批次首现项向量生成失败，未入库',
                        source_path=dup_item.source_path,
                    )
                continue

            ext = normalized_ext(item.source_path)
            web_path, fs_path = storage_paths(
                upload_folder, item.model_number, item.content_hash, ext
            )
            os.makedirs(os.path.dirname(fs_path), exist_ok=True)
            with open(item.source_path, 'rb') as src, open(fs_path, 'wb') as dst:
                dst.write(src.read())

            db.session.add(ProductImage(
                model_number=item.model_number,
                image_path=web_path,
                vector=vector.tolist(),
                content_hash=item.content_hash,
                original_path=os.path.abspath(item.source_path),
                image_order=item.image_order,
                is_primary=item.is_primary,
            ))
            results[index] = IngestResult(
                model_number=item.model_number, content_hash=item.content_hash,
                status='created', image_path=web_path, source_path=item.source_path,
                fs_path=fs_path,
            )
            for dup_index in dup_indices:
                dup_item = pending[dup_index]
                results[dup_index] = IngestResult(
                    model_number=dup_item.model_number, content_hash=dup_item.content_hash,
                    status='duplicate', duplicate_of=web_path, source_path=dup_item.source_path,
                )

        return results


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning('清理文件失败 path=%s error=%s', path, exc)
