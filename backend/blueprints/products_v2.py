"""
Products V2 Blueprint - 电子产品配件管理 API
适配新的数据库结构：使用 model_number 作为主键
"""
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import os
import tempfile
import uuid
import json
import csv
import io
from datetime import datetime
from urllib.parse import quote
from flask_cors import cross_origin
from sqlalchemy.exc import IntegrityError
from models import db, ImageAsset, Product
from product_search import EmbeddingServiceError, VectorSearchError
from services.asset_ingest import (
    AssetIngestConflictError,
    AssetIngestError,
    ImageAssetIngestService,
)
from services.image_normalizer import ImageNormalizationError
from services import legacy_product_images
from services.object_storage import ObjectStorageError, OssObjectStorage
from services.upload_source import prepare_multipart_source

products_v2_bp = Blueprint('products_v2', __name__, url_prefix='/api/products')

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

PRODUCT_UPLOAD_SOURCE_PROVIDER = 'product-upload'
PRODUCT_UPLOAD_SOURCE_BUCKET = 'product-uploads'

# ========================================
# 辅助函数
# ========================================

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
        os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def error_response(message, error_code, status_code):
    """统一错误响应格式"""
    return jsonify({'error': message, 'error_code': error_code}), status_code


def validate_top_k(raw_top_k, default=10, min_value=1, max_value=50):
    """严格校验 top_k 参数"""
    if raw_top_k is None or raw_top_k == '':
        return default

    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError) as exc:
        raise ValueError('top_k 必须是整数') from exc

    if not (min_value <= top_k <= max_value):
        raise ValueError(f'top_k 必须在 {min_value} 到 {max_value} 之间')

    return top_k


def get_asset_ingest_service(source):
    """构造统一图片资产入库服务，并只在外部系统边界提供测试替身。"""
    storage = current_app.config.get('IMAGE_ASSET_STORAGE')
    if storage is None:
        storage = OssObjectStorage.from_env()
    return ImageAssetIngestService(
        source=source,
        storage=storage,
        embedding_client=current_app.config.get('IMAGE_INGEST_EMBEDDING'),
        normalizer=current_app.config.get('IMAGE_ASSET_NORMALIZER'),
        source_provider=PRODUCT_UPLOAD_SOURCE_PROVIDER,
    )


def prepare_product_uploads(image_files, model_number):
    """把 multipart 图片包装成可重放的只读来源。

    Product 请求不是分布式事务：OSS 上传成功后，embedding 或数据库提交仍可能
    失败。来源路径因此必须由商品、内容、文件名和同名出现序号稳定推导；重试同一
    请求会 HEAD 并复用原对象，而不会因随机 UUID 再制造无法关联的 OSS 孤儿。
    """
    encoded_model_number = quote(str(model_number), safe='')
    # 与输入文件列表等长对齐：被扩展名白名单跳过的文件占 None 槽位，
    # 保证调用方能按原始下标定位每个上传文件的入库结果。
    allowed_slots = [
        bool(image_file) and allowed_file(image_file.filename or '')
        for image_file in image_files
    ]
    source, relative_paths = prepare_multipart_source(
        image_files,
        source_bucket=PRODUCT_UPLOAD_SOURCE_BUCKET,
        is_allowed=allowed_file,
        build_relative_path=lambda filename, content_hash, occurrence: (
            f'models/{encoded_model_number}/{content_hash}/'
            f'{occurrence:04d}/{filename}'
        ),
    )
    positions = iter(relative_paths)
    aligned_paths = [
        next(positions) if allowed else None for allowed in allowed_slots
    ]
    return source, relative_paths, aligned_paths


def attach_product_upload_result(result, model_number):
    """关联 active 上传结果；回收站命中只返回导航信息。

    删除 Product 会把资产型号置空，之后的 active 幂等重试可重新关联到调用方
    显式提交的型号。归档资产保持原生命周期与关联，不在导入路径中自动恢复。
    """
    if (
        result.status not in {'created', 'existing', 'in_recycle_bin'}
        or not result.asset_id
    ):
        return None

    asset = db.session.get(ImageAsset, result.asset_id)
    if asset is None:
        raise AssetIngestError(
            '图片资产写入结果无法回读',
            stage='database',
        )
    if result.status == 'in_recycle_bin':
        if asset.status != 'archived':
            raise AssetIngestConflictError(
                '图片资产生命周期已变化，请重新上传以获取最新结果',
                stage='database',
                kind='version_conflict',
                asset_id=str(asset.id),
                source_relative_path=result.source_relative_path,
            )
        return {
            'asset_id': result.asset_id,
            'source_relative_path': result.source_relative_path,
            'status': result.status,
            'recovery_action': result.recovery_action,
        }

    if asset.status != 'active':
        raise AssetIngestConflictError(
            '图片资产生命周期已变化，请重新上传以获取最新结果',
            stage='database',
            kind='version_conflict',
            asset_id=str(asset.id),
            source_relative_path=result.source_relative_path,
        )
    if asset.model_number not in {None, model_number}:
        raise AssetIngestConflictError(
            '图片资产已经关联到其他商品',
            stage='database',
            kind='assignment_conflict',
            asset_id=str(asset.id),
            source_relative_path=result.source_relative_path,
        )

    asset.model_number = model_number
    return {
        'asset_id': result.asset_id,
        'source_relative_path': result.source_relative_path,
        'status': result.status,
    }


def summarize_product_upload_results(image_results):
    """保留旧计数字段，并显式区分新建与幂等复用。"""
    created = [
        item for item in image_results
        if item['status'] == 'created'
    ]
    reused = [
        item for item in image_results
        if item['status'] == 'existing'
    ]
    recycle_bin = [
        item for item in image_results
        if item['status'] == 'in_recycle_bin'
    ]
    return {
        'uploaded_images': len(created),
        'reused_images': len(reused),
        'recycle_bin_images': len(recycle_bin),
        'skipped_duplicates': [item['asset_id'] for item in reused],
        'image_results': image_results,
    }


def asset_ingest_conflict_response(error):
    """把来源冲突与存储/版本冲突映射为稳定且脱敏的 409。"""
    if error.kind == 'source_conflict':
        item = {
            'asset_id': error.asset_id,
            'source_relative_path': error.source_relative_path,
            'status': 'source_conflict',
        }
        return jsonify({
            'error': '来源冲突：同一来源身份已存在不同内容，未覆盖现有资产',
            'error_code': 'IMAGE_ASSET_SOURCE_CONFLICT',
            'image_results': [item],
        }), 409
    return error_response(
        '图片资产发生冲突，未覆盖现有内容',
        'IMAGE_ASSET_CONFLICT',
        409,
    )


def image_asset_for_product(asset, image_order=0):
    """适配现有商品管理界面的图片字段，不生成或持久化 OSS 签名 URL。"""
    preview_url = f'/api/image-assets/{asset.id}/preview'
    return {
        'id': str(asset.id),
        'asset_id': str(asset.id),
        'model_number': asset.model_number,
        'image_path': preview_url,
        'preview_url': preview_url,
        'display_name': asset.display_name,
        'source_relative_path': asset.source_relative_path,
        'version': asset.version,
        'content_hash': asset.content_hash,
        'original_path': None,
        'image_order': image_order,
        'is_primary': image_order == 0,
        'created_at': asset.created_at.isoformat() if asset.created_at else None,
    }


def apply_product_image_order(model_number, ordered_asset_ids):
    """按给定顺序重写商品活动资产的 sort_order（0 起连续）。

    未出现在列表中的本商品资产按当前顺序追加到队尾；列表中不存在、
    不属于本商品或重复的条目会被跳过。
    """
    assets = ImageAsset.query.filter(
        ImageAsset.model_number == model_number,
        ImageAsset.status == 'active',
    ).order_by(
        ImageAsset.sort_order,
        ImageAsset.created_at,
        ImageAsset.id,
    ).all()
    assets_by_id = {str(asset.id): asset for asset in assets}
    position = 0
    seen = set()
    for asset_id in ordered_asset_ids:
        if asset_id in seen:
            continue
        asset = assets_by_id.get(asset_id)
        if asset is None:
            continue
        asset.sort_order = position
        seen.add(asset_id)
        position += 1
    for asset in assets:
        if str(asset.id) in seen:
            continue
        asset.sort_order = position
        position += 1


def products_with_active_images(products):
    """批量拼装商品与活动资产，避免产品列表出现逐行图片查询。"""
    if not products:
        return []

    model_numbers = [product.model_number for product in products]
    assets_by_model = {model_number: [] for model_number in model_numbers}
    assets = ImageAsset.query.filter(
        ImageAsset.model_number.in_(model_numbers),
        ImageAsset.status == 'active',
    ).order_by(
        ImageAsset.model_number,
        ImageAsset.sort_order,
        ImageAsset.created_at,
        ImageAsset.id,
    ).all()
    for asset in assets:
        assets_by_model[asset.model_number].append(asset)

    result = []
    for product in products:
        product_dict = product.to_dict()
        product_dict['images'] = [
            image_asset_for_product(asset, image_order=index)
            for index, asset in enumerate(
                assets_by_model[product.model_number]
            )
        ]
        result.append(product_dict)
    return result


def legacy_images_require_migration():
    """检查退休图片表，非空时阻止会触发级联删除的商品写操作。"""
    audit = legacy_product_images.audit_legacy_product_images(
        db.session.connection()
    )
    return audit.compatibility_required


def legacy_images_error_response():
    return error_response(
        '检测到旧商品图片数据，请先制定兼容迁移方案后再删除商品',
        'LEGACY_PRODUCT_IMAGES_REQUIRE_MIGRATION',
        409,
    )


def asset_ingest_error_response(error):
    """把统一入库服务的内部阶段转换为稳定的外部错误。"""
    if error.stage == 'embedding':
        return error_response(
            '图片识别服务暂不可用，请稍后重试',
            'EMBEDDING_SERVICE_ERROR',
            503,
        )
    if error.stage in {'original', 'preview'}:
        return error_response(
            '图片存储服务暂不可用，请稍后重试',
            'OBJECT_STORAGE_ERROR',
            503,
        )
    return error_response(
        '图片资产写入失败',
        'IMAGE_ASSET_INGEST_FAILED',
        500,
    )


# ========================================
# 基础 CRUD API
# ========================================

@products_v2_bp.route('', methods=['GET'])
@cross_origin()
def get_products():
    """获取产品列表（支持分页和筛选）"""
    try:
        # 分页参数
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)

        # 筛选参数
        category = request.args.get('category')
        search = request.args.get('search')  # 搜索型号或分类

        # 构建查询
        query = Product.query

        if category:
            query = query.filter(Product.category == category)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                db.or_(
                    Product.model_number.like(search_pattern),
                    Product.category.like(search_pattern)
                )
            )

        # 排序和分页
        query = query.order_by(Product.created_at.desc())

        # 执行分页查询
        if page > 0:
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)
            products = pagination.items
            total = pagination.total
        else:
            # 不分页，返回所有
            products = query.all()
            total = len(products)

        product_list = products_with_active_images(products)

        return jsonify({
            'products': product_list,
            'total': total,
            'page': page,
            'per_page': per_page
        })

    except Exception as e:
        current_app.logger.error(f"获取产品列表失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('/<model_number>', methods=['GET'])
@cross_origin()
def get_product(model_number):
    """获取单个产品详情"""
    try:
        product = db.session.get(Product, model_number)

        if not product:
            return jsonify({'error': '产品不存在'}), 404

        return jsonify(products_with_active_images([product])[0])

    except Exception as e:
        current_app.logger.error(f"获取产品详情失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('', methods=['POST'])
@cross_origin()
def create_product():
    """创建新产品（支持图片上传）"""
    try:
        # 获取产品数据
        product_data_str = request.form.get('product')
        if not product_data_str:
            return jsonify({'error': '缺少产品数据'}), 400

        product_data = json.loads(product_data_str)

        # 创建产品时仅型号必填；其余 NOT NULL 字段缺省时以空字符串占位，
        # 可稍后在产品编辑页补全。
        if not product_data.get('model_number'):
            return jsonify({'error': '缺少必填字段: model_number'}), 400

        model_number = product_data['model_number']

        # 检查型号是否已存在
        if db.session.get(Product, model_number):
            return jsonify({'error': f'型号 {model_number} 已存在'}), 400

        # 创建产品对象（单事务：此处不提交）
        product = Product.from_dict(product_data)
        for field in ('photographer_file', 'alibaba_product_url', 'category'):
            if getattr(product, field) is None:
                setattr(product, field, '')
        db.session.add(product)

        source, relative_paths, aligned_paths = prepare_product_uploads(
            request.files.getlist('images'),
            model_number,
        )
        image_results = []
        # 与 multipart images 顺序对齐；被跳过或未入库的槽位为 None
        ingested_asset_ids = []
        request_id = uuid.uuid4().hex

        if relative_paths:
            ingest_service = get_asset_ingest_service(source)
            for aligned_path in aligned_paths:
                if aligned_path is None:
                    ingested_asset_ids.append(None)
                    continue
                result = ingest_service.ingest_one(
                    aligned_path,
                    model_number=model_number,
                    request_id=request_id,
                    commit=False,
                )
                image_result = attach_product_upload_result(
                    result,
                    model_number,
                )
                if image_result:
                    image_results.append(image_result)
                    ingested_asset_ids.append(str(result.asset_id))
                else:
                    ingested_asset_ids.append(None)

        ordered_new_ids = [asset_id for asset_id in ingested_asset_ids if asset_id]
        if ordered_new_ids:
            apply_product_image_order(model_number, ordered_new_ids)

        db.session.commit()

        return jsonify({
            'message': '产品创建成功',
            'model_number': model_number,
            **summarize_product_upload_results(image_results),
        }), 201

    except AssetIngestConflictError as e:
        db.session.rollback()
        current_app.logger.warning(
            '创建产品失败（图片资产冲突）: %s',
            type(e).__name__,
        )
        return asset_ingest_conflict_response(e)
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning(
            '创建产品失败（数据库冲突）: %s',
            type(e).__name__,
        )
        return error_response(
            '产品或图片资产发生并发冲突，请重试',
            'PRODUCT_WRITE_CONFLICT',
            409,
        )
    except ImageNormalizationError:
        db.session.rollback()
        return error_response(
            '上传图片已损坏或无法安全解码',
            'INVALID_IMAGE',
            400,
        )
    except EmbeddingServiceError as e:
        db.session.rollback()
        current_app.logger.error(
            '创建产品失败（向量服务） error_type=%s',
            type(e).__name__,
        )
        return error_response(
            '图片识别服务暂不可用，请稍后重试',
            'EMBEDDING_SERVICE_ERROR',
            503,
        )
    except ObjectStorageError as e:
        db.session.rollback()
        current_app.logger.error(
            '创建产品失败（对象存储） error_type=%s',
            type(e).__name__,
        )
        return error_response(
            '图片存储服务暂不可用，请稍后重试',
            'OBJECT_STORAGE_ERROR',
            503,
        )
    except AssetIngestError as e:
        db.session.rollback()
        current_app.logger.error(
            '创建产品失败（图片资产） stage=%s error_type=%s',
            e.stage,
            type(e).__name__,
        )
        return asset_ingest_error_response(e)
    except RequestEntityTooLarge:
        db.session.rollback()
        return error_response('上传图片过大', 'IMAGE_TOO_LARGE', 413)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            '创建产品失败 error_type=%s',
            type(e).__name__,
        )
        return error_response('产品创建失败', 'PRODUCT_CREATE_FAILED', 500)


@products_v2_bp.route('/<model_number>', methods=['PUT'])
@cross_origin()
def update_product(model_number):
    """更新产品信息"""
    try:
        product = db.session.get(Product, model_number)
        if not product:
            return jsonify({'error': '产品不存在'}), 404

        # 获取更新数据
        image_order_raw = None
        product_data_str = request.form.get('product')
        if product_data_str:
            product_data = json.loads(product_data_str)
            image_order_raw = product_data.get('image_order')

            # 更新字段（排除主键）
            for key, value in product_data.items():
                if key != 'model_number' and hasattr(product, key):
                    setattr(product, key, value)

        if image_order_raw is not None and (
            not isinstance(image_order_raw, list)
            or any(not isinstance(entry, str) for entry in image_order_raw)
        ):
            db.session.rollback()
            return error_response('图片排序参数无效', 'INVALID_IMAGE_ORDER', 400)

        existing_ordered_ids = [
            str(asset.id)
            for asset in ImageAsset.query.filter(
                ImageAsset.model_number == model_number,
                ImageAsset.status == 'active',
            ).order_by(
                ImageAsset.sort_order,
                ImageAsset.created_at,
                ImageAsset.id,
            ).all()
        ]

        source, relative_paths, aligned_paths = prepare_product_uploads(
            request.files.getlist('images'),
            model_number,
        )
        image_results = []
        # 与 multipart images 顺序对齐；被跳过或未入库的槽位为 None
        ingested_asset_ids = []
        request_id = uuid.uuid4().hex

        if relative_paths:
            ingest_service = get_asset_ingest_service(source)
            for aligned_path in aligned_paths:
                if aligned_path is None:
                    ingested_asset_ids.append(None)
                    continue
                result = ingest_service.ingest_one(
                    aligned_path,
                    model_number=model_number,
                    request_id=request_id,
                    commit=False,
                )
                image_result = attach_product_upload_result(
                    result,
                    model_number,
                )
                if image_result:
                    image_results.append(image_result)
                    ingested_asset_ids.append(str(result.asset_id))
                else:
                    ingested_asset_ids.append(None)

        if image_order_raw is not None:
            resolved_order = []
            for entry in image_order_raw:
                if entry.startswith('new:'):
                    index_text = entry[4:]
                    if index_text.isdigit():
                        index = int(index_text)
                        if index < len(ingested_asset_ids):
                            asset_id = ingested_asset_ids[index]
                            if asset_id:
                                resolved_order.append(asset_id)
                    continue
                resolved_order.append(entry)
            apply_product_image_order(model_number, resolved_order)
        else:
            ordered_new_ids = [
                asset_id for asset_id in ingested_asset_ids if asset_id
            ]
            if ordered_new_ids:
                apply_product_image_order(
                    model_number,
                    existing_ordered_ids + ordered_new_ids,
                )

        db.session.commit()

        return jsonify({
            'message': '产品更新成功',
            **summarize_product_upload_results(image_results),
        })

    except AssetIngestConflictError as e:
        db.session.rollback()
        current_app.logger.warning(
            '更新产品失败（图片资产冲突）: %s',
            type(e).__name__,
        )
        return asset_ingest_conflict_response(e)
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning(
            '更新产品失败（数据库冲突）: %s',
            type(e).__name__,
        )
        return error_response(
            '产品或图片资产发生并发冲突，请重试',
            'PRODUCT_WRITE_CONFLICT',
            409,
        )
    except ImageNormalizationError:
        db.session.rollback()
        return error_response(
            '上传图片已损坏或无法安全解码',
            'INVALID_IMAGE',
            400,
        )
    except EmbeddingServiceError as e:
        db.session.rollback()
        current_app.logger.error(
            '更新产品失败（向量服务） error_type=%s',
            type(e).__name__,
        )
        return error_response(
            '图片识别服务暂不可用，请稍后重试',
            'EMBEDDING_SERVICE_ERROR',
            503,
        )
    except ObjectStorageError as e:
        db.session.rollback()
        current_app.logger.error(
            '更新产品失败（对象存储） error_type=%s',
            type(e).__name__,
        )
        return error_response(
            '图片存储服务暂不可用，请稍后重试',
            'OBJECT_STORAGE_ERROR',
            503,
        )
    except AssetIngestError as e:
        db.session.rollback()
        current_app.logger.error(
            '更新产品失败（图片资产） stage=%s error_type=%s',
            e.stage,
            type(e).__name__,
        )
        return asset_ingest_error_response(e)
    except RequestEntityTooLarge:
        db.session.rollback()
        return error_response('上传图片过大', 'IMAGE_TOO_LARGE', 413)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            '更新产品失败 error_type=%s',
            type(e).__name__,
        )
        return error_response('产品更新失败', 'PRODUCT_UPDATE_FAILED', 500)


@products_v2_bp.route('/<model_number>', methods=['DELETE'])
@cross_origin()
def delete_product(model_number):
    """删除产品"""
    try:
        product = db.session.get(Product, model_number)
        if not product:
            return jsonify({'error': '产品不存在'}), 404

        if legacy_images_require_migration():
            return legacy_images_error_response()

        db.session.delete(product)
        db.session.commit()

        return jsonify({'message': '产品删除成功'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            '删除产品失败 model_number=%s error_type=%s',
            model_number,
            type(e).__name__,
        )
        return error_response(
            '产品删除失败，请稍后重试',
            'PRODUCT_DELETE_FAILED',
            500,
        )


@products_v2_bp.route('/batch-delete', methods=['POST'])
@cross_origin()
def batch_delete_products():
    """批量删除产品"""
    try:
        data = request.get_json()
        if not data or 'model_numbers' not in data:
            return jsonify({'error': '缺少 model_numbers 参数'}), 400

        model_numbers = data['model_numbers']
        if not isinstance(model_numbers, list):
            return jsonify({'error': 'model_numbers 必须是数组'}), 400

        if legacy_images_require_migration():
            return legacy_images_error_response()

        # 批量删除
        deleted_count = Product.query.filter(
            Product.model_number.in_(model_numbers)
        ).delete(synchronize_session=False)

        db.session.commit()

        return jsonify({
            'message': f'成功删除 {deleted_count} 个产品',
            'deleted_count': deleted_count
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            '批量删除产品失败 error_type=%s',
            type(e).__name__,
        )
        return error_response(
            '批量删除产品失败，请稍后重试',
            'PRODUCT_BATCH_DELETE_FAILED',
            500,
        )


# ========================================
# 图片管理 API
# ========================================

@products_v2_bp.route('/<model_number>/images/<uuid:asset_id>', methods=['DELETE'])
@cross_origin()
def delete_product_image(model_number, asset_id):
    """归档产品图片资产，不删除数据库记录或共享 OSS 对象。"""
    try:
        asset = ImageAsset.query.filter_by(
            id=asset_id,
            model_number=model_number
        ).first()

        if not asset:
            return jsonify({'error': '图片不存在'}), 404

        if asset.status != 'archived':
            asset.status = 'archived'
            asset.archived_at = datetime.now()
        db.session.commit()

        return jsonify({'message': '图片已归档'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            '归档图片失败 asset_id=%s error_type=%s',
            asset_id,
            type(e).__name__,
        )
        return error_response('图片归档失败', 'IMAGE_ARCHIVE_FAILED', 500)


# ========================================
# CSV 批量导入 API（完全重写）
# ========================================

@products_v2_bp.route('/import-csv', methods=['POST'])
@cross_origin()
def import_csv():
    """
    CSV 批量导入产品
    支持新的字段结构：model_number, photographer_file, alibaba_product_url, category 等
    """
    try:
        # 检查文件
        if 'csv_file' not in request.files:
            return jsonify({'error': '缺少 CSV 文件'}), 400

        csv_file = request.files['csv_file']
        if not csv_file.filename.endswith('.csv'):
            return jsonify({'error': '文件必须是 CSV 格式'}), 400

        # 读取 CSV 内容（支持多种编码）
        csv_content_bytes = csv_file.read()
        csv_content = None

        encodings = ['utf-8', 'gbk', 'gb2312', 'utf-8-sig']
        for encoding in encodings:
            try:
                csv_content = csv_content_bytes.decode(encoding)
                current_app.logger.info(f"使用 {encoding} 编码读取 CSV")
                break
            except UnicodeDecodeError:
                continue

        if csv_content is None:
            return jsonify({'error': '无法解码 CSV 文件，请使用 UTF-8 或 GBK 编码'}), 400

        # 解析 CSV
        rows = list(csv.DictReader(io.StringIO(csv_content)))

        stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'errors': []}

        REQUIRED_FIELDS = ['model_number', 'photographer_file', 'alibaba_product_url', 'category']
        OPTIONAL_FIELDS = [
            'spec_cn_reference', 'spec_cn', 'spec_en',
            'product_size', 'package_size',
            'price_1688', 'fob_price_tier1', 'fob_price_tier2', 'fob_price_tier3',
            'intl_platform_price', 'competitor_price',
            'ref_link_1', 'ref_link_2', 'ref_link_3',
            'intl_platform_url', 'intl_platform_url_1', 'intl_platform_url_2'
        ]
        NUMERIC_SUFFIXES = ('price_1688', 'fob_price_tier1', 'fob_price_tier2',
                            'fob_price_tier3', 'intl_platform_price', 'competitor_price')
        COMMIT_EVERY = 200

        # 一次查出全部已存在型号，替代逐行 query.get
        candidate_model_numbers = {
            (row.get('model_number') or '').strip()
            for row in rows if (row.get('model_number') or '').strip()
        }
        existing_model_numbers = {
            value for (value,) in db.session.query(Product.model_number).filter(
                Product.model_number.in_(candidate_model_numbers)
            ).all()
        } if candidate_model_numbers else set()

        def flush_pending_batch(pending):
            """提交当前累积批次（commit 失败时整体回滚，绝不让异常冒泡出函数）。

            success 只能在这里增加——批内任意一行触发数据库层错误都会让整批
            原子回滚，如果在 db.session.add() 时就计入 success，会出现
            "stats 报告成功、但库里实际是空的" 的错配（fix round 1 修复的问题）。
            commit 失败时把批内全部行计入 failed，并把它们的 model_number 从
            existing_model_numbers 中撤销——否则同一 CSV 里稍后出现的同型号
            会被误判为"已存在"而跳过，但它其实从未真正入库过。
            这个函数内部吞掉所有异常，因此收尾提交也必须走这里，不能让数据库层
            错误冒泡到最外层 except，把 200+stats 的响应变成 500+error
            （同样是 fix round 1 修复的问题）。
            """
            if not pending:
                return
            try:
                db.session.commit()
                stats['success'] += len(pending)
            except Exception as e:
                db.session.rollback()
                for _, mn in pending:
                    existing_model_numbers.discard(mn)
                stats['failed'] += len(pending)
                first_row, last_row = pending[0][0], pending[-1][0]
                if first_row == last_row:
                    error_msg = f"第{first_row}行提交失败: {str(e)}"
                else:
                    error_msg = (
                        f"第{first_row}~{last_row}行整批提交失败（批量提交，"
                        f"具体出错行需自查）: {str(e)}"
                    )
                stats['errors'].append(error_msg)
                current_app.logger.error(error_msg)

        pending_rows = []  # [(row_number, model_number), ...]：本批已 add 但未 commit
        for row_number, row in enumerate(rows, start=2):  # 第 1 行是表头
            stats['total'] += 1

            try:
                for field in REQUIRED_FIELDS:
                    if not row.get(field) or str(row.get(field)).strip() == '':
                        raise ValueError(f'缺少必填字段: {field}')

                model_number = row['model_number'].strip()

                # 同时覆盖「库里已有」与「同一个 CSV 内重复」
                if model_number in existing_model_numbers:
                    stats['skipped'] += 1
                    stats['errors'].append(f"第{row_number}行: 型号 {model_number} 已存在，跳过")
                    continue

                product_data = {
                    'model_number': model_number,
                    'photographer_file': row.get('photographer_file', '').strip(),
                    'alibaba_product_url': row.get('alibaba_product_url', '').strip(),
                    'category': row.get('category', '').strip(),
                }

                for field in OPTIONAL_FIELDS:
                    value = (row.get(field) or '').strip()
                    if not value:
                        continue
                    if field in NUMERIC_SUFFIXES:
                        try:
                            product_data[field] = float(value)
                        except ValueError:
                            current_app.logger.warning(f"第{row_number}行: {field} 值无效: {value}")
                    else:
                        product_data[field] = value

                db.session.add(Product.from_dict(product_data))
                existing_model_numbers.add(model_number)
                # 注意：这里不增加 stats['success']——必须等 flush_pending_batch
                # 真正 commit 成功后才能算数，否则批量提交失败时 success 会跟
                # 实际入库行数对不上（fix round 1 的 Critical 1）。
                pending_rows.append((row_number, model_number))

            except ValueError as e:
                # 纯业务校验失败（缺必填字段等），发生在 db.session.add() 之前，
                # session 未被这一行污染，不影响 pending_rows，不需要 rollback。
                stats['failed'] += 1
                error_msg = f"第{row_number}行: {str(e)}"
                stats['errors'].append(error_msg)
                current_app.logger.error(error_msg)
                continue

            if len(pending_rows) >= COMMIT_EVERY:
                flush_pending_batch(pending_rows)
                pending_rows = []

        # 收尾：提交最后一批不足 COMMIT_EVERY 的数据。必须复用 flush_pending_batch——
        # 数据库层错误在这里会被吞掉、转成 stats['failed']，而不会冒泡到最外层
        # except 把响应变成 500 + {'error': ...}（fix round 1 的 Critical 2）。
        flush_pending_batch(pending_rows)

        return jsonify({
            'message': 'CSV 导入完成',
            'stats': stats
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"CSV 导入失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('/csv-template', methods=['GET'])
@cross_origin()
def download_csv_template():
    """下载 CSV 导入模板"""
    template_content = """model_number,photographer_file,alibaba_product_url,category,spec_cn,spec_en,product_size,package_size,price_1688,fob_price_tier1,fob_price_tier2,fob_price_tier3,intl_platform_price,competitor_price,ref_link_1,ref_link_2,ref_link_3,intl_platform_url,intl_platform_url_1,intl_platform_url_2
CS-001,photographer_001,https://detail.1688.com/offer/123456.html,相机肩带,纯棉材质 3.8cm宽,Cotton Material 3.8cm Width,120cm x 3.8cm,15cm x 8cm x 3cm,15.80,2.50,2.20,1.90,3.50,3.20,,,,,,,
HL-002,photographer_002,https://detail.1688.com/offer/654321.html,相机挂绳,尼龙编织 1cm宽,Nylon 1cm Width,80cm x 1cm,12cm x 5cm x 2cm,8.50,1.20,1.00,0.85,2.00,1.80,,,,,,,
"""

    return Response(
        template_content,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=product_import_template.csv'}
    )


# ========================================
# 向量索引构建 API（SSE 流式）
# ========================================

@products_v2_bp.route('/build-vector-index', methods=['GET'])
@cross_origin()
def build_vector_index():
    """
    (Deprecated) 构建向量索引
    此接口在 Stateless 架构下已不再需要，保留仅为兼容性或手动触发向量生成（如果需要）
    目前主要用于确保旧数据有向量
    """
    return jsonify({'message': 'Stateless architecture does not need explicit index building.'}), 200


# ========================================
# 图片搜索 API
# ========================================

@products_v2_bp.route('/search', methods=['POST'])
@cross_origin()
def search_products():
    """以图搜图功能"""
    request_id = uuid.uuid4().hex
    try:
        # 检查搜索服务
        if 'PRODUCT_SEARCH_SERVICE' not in current_app.config:
            return error_response('向量搜索未配置', 'VECTOR_SEARCH_NOT_CONFIGURED', 500)

        search_service = current_app.config['PRODUCT_SEARCH_SERVICE']

        try:
            top_k = validate_top_k(request.form.get('top_k'))
        except ValueError as e:
            return error_response(str(e), 'INVALID_TOP_K', 400)

        # 处理上传的图片
        if 'image' not in request.files:
            return error_response('缺少图片文件', 'MISSING_IMAGE_FILE', 400)

        image_file = request.files['image']
        if not image_file or not allowed_file(image_file.filename):
            return error_response('图片格式不支持', 'UNSUPPORTED_IMAGE_FORMAT', 400)

        suffix = os.path.splitext(secure_filename(image_file.filename))[1]
        with tempfile.TemporaryDirectory(prefix='image-query-upload-') as temp_dir:
            temp_path = os.path.join(temp_dir, f'query{suffix}')
            image_file.save(temp_path)
            results = search_service.search_similar_images(temp_path, top_k=top_k, request_id=request_id)
            return jsonify(results)

    except RequestEntityTooLarge:
        return error_response(
            '查询图片过大，请缩小后重试',
            'IMAGE_TOO_LARGE',
            413,
        )
    except ImageNormalizationError:
        return error_response(
            '查询图片已损坏或无法安全解码',
            'INVALID_IMAGE',
            400,
        )
    except EmbeddingServiceError as e:
        current_app.logger.error(
            '图片搜索失败（向量服务） request_id=%s error_type=%s',
            request_id,
            type(e).__name__,
        )
        return error_response(
            '图片识别服务暂不可用，请稍后重试',
            'EMBEDDING_SERVICE_ERROR',
            503,
        )
    except VectorSearchError as e:
        current_app.logger.error(
            '图片搜索失败（向量检索） request_id=%s error_type=%s',
            request_id,
            type(e).__name__,
        )
        return error_response(
            '图片检索服务暂不可用，请稍后重试',
            'VECTOR_SEARCH_ERROR',
            500,
        )
    except Exception as e:
        current_app.logger.error(
            '图片搜索失败 request_id=%s error_type=%s',
            request_id,
            type(e).__name__,
        )
        return error_response(
            '图片搜索失败，请稍后重试',
            'IMAGE_SEARCH_FAILED',
            500,
        )


# ========================================
# 统计 API
# ========================================

@products_v2_bp.route('/statistics', methods=['GET'])
@cross_origin()
def get_statistics():
    """获取产品统计信息"""
    try:
        total_products = Product.query.count()
        total_images = ImageAsset.query.filter(
            ImageAsset.status == 'active',
            ImageAsset.model_number.isnot(None),
        ).count()

        # 按分类统计
        category_stats = db.session.query(
            Product.category,
            db.func.count(Product.model_number)
        ).group_by(Product.category).all()

        return jsonify({
            'total_products': total_products,
            'total_images': total_images,
            'categories': [{'name': cat, 'count': count} for cat, count in category_stats]
        })

    except Exception as e:
        current_app.logger.error(f"获取统计信息失败: {str(e)}")
        return jsonify({'error': str(e)}), 500
