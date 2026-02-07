"""
Products V2 Blueprint - 电子产品配件管理 API
适配新的数据库结构：使用 model_number 作为主键
"""
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context
from werkzeug.utils import secure_filename
import os
import uuid
import json
import csv
import io
from flask_cors import cross_origin
from models import db, Product, ProductImage
from product_search import EmbeddingServiceError, VectorSearchError
products_v2_bp = Blueprint('products_v2', __name__, url_prefix='/api/products')

# ========================================
# 辅助函数
# ========================================

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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


def dedupe_results_by_model_number(results):
    """按 model_number 去重，保留最高相似度。"""
    deduped = []
    seen_model_numbers = set()

    for result in results:
        model_number = result.get('model_number')
        if not model_number or model_number in seen_model_numbers:
            continue
        deduped.append(result)
        seen_model_numbers.add(model_number)

    return deduped


def save_product_image(file, model_number):
    """
    保存产品图片到本地
    返回: (web_path, filesystem_path)
    """
    if not file or not allowed_file(file.filename):
        return None, None

    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4()}_{filename}"

    # 创建按型号组织的目录
    product_dir = os.path.join(
        current_app.config['UPLOAD_FOLDER'],
        'product_images',
        model_number
    )
    os.makedirs(product_dir, exist_ok=True)

    # 保存文件
    filesystem_path = os.path.join(product_dir, unique_filename)
    file.save(filesystem_path)

    # 生成Web访问路径
    web_path = f"/uploads/product_images/{model_number}/{unique_filename}"

    return web_path, filesystem_path


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

        # 转换为字典
        product_list = [product.to_dict() for product in products]

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
        product = Product.query.get(model_number)

        if not product:
            return jsonify({'error': '产品不存在'}), 404

        # 获取产品图片列表
        product_dict = product.to_dict()
        product_dict['images'] = [img.to_dict() for img in product.images]

        return jsonify(product_dict)

    except Exception as e:
        current_app.logger.error(f"获取产品详情失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('', methods=['POST'])
@cross_origin()
def create_product():
    """创建新产品（支持图片上传）"""
    saved_filesystem_paths = []
    should_cleanup_files = False
    try:
        # 获取产品数据
        product_data_str = request.form.get('product')
        if not product_data_str:
            return jsonify({'error': '缺少产品数据'}), 400

        product_data = json.loads(product_data_str)

        # 验证必填字段
        required_fields = ['model_number', 'photographer_file', 'alibaba_product_url', 'category']
        for field in required_fields:
            if not product_data.get(field):
                return jsonify({'error': f'缺少必填字段: {field}'}), 400

        model_number = product_data['model_number']

        # 检查型号是否已存在
        if Product.query.get(model_number):
            return jsonify({'error': f'型号 {model_number} 已存在'}), 400

        # 创建产品对象（单事务：此处不提交）
        product = Product.from_dict(product_data)
        db.session.add(product)

        # 处理图片上传
        images = request.files.getlist('images')
        uploaded_images = []
        for idx, image_file in enumerate(images):
            if image_file and allowed_file(image_file.filename):
                web_path, filesystem_path = save_product_image(image_file, model_number)

                if web_path and filesystem_path:
                    saved_filesystem_paths.append(filesystem_path)
                    # 生成向量
                    product_search_service = current_app.config.get('PRODUCT_SEARCH_SERVICE')
                    if product_search_service:
                        request_id = uuid.uuid4().hex
                        feature = product_search_service.extract_feature(filesystem_path, request_id=request_id)

                        # 创建 ProductImage 记录
                        product_image = ProductImage(
                            model_number=model_number,
                            image_path=web_path,
                            vector=feature.tolist(),
                            original_path=filesystem_path,
                            image_order=idx,
                            is_primary=(idx == 0)  # 第一张为主图
                        )
                        db.session.add(product_image)
                        uploaded_images.append(web_path)

        db.session.commit()
        should_cleanup_files = False

        # 无需刷新向量索引 (Stateless)
        # if uploaded_images and current_app.config.get('PRODUCT_INDEX'):
        #     current_app.config['PRODUCT_INDEX'].refresh_from_database()

        return jsonify({
            'message': '产品创建成功',
            'model_number': model_number,
            'uploaded_images': len(uploaded_images)
        }), 201

    except EmbeddingServiceError as e:
        db.session.rollback()
        should_cleanup_files = True
        current_app.logger.error(f"创建产品失败（向量服务）: {str(e)}")
        return error_response(str(e), 'EMBEDDING_SERVICE_ERROR', 503)
    except Exception as e:
        db.session.rollback()
        should_cleanup_files = True
        current_app.logger.error(f"创建产品失败: {str(e)}")
        return error_response(str(e), 'PRODUCT_CREATE_FAILED', 500)
    finally:
        if should_cleanup_files:
            for filesystem_path in saved_filesystem_paths:
                try:
                    if os.path.exists(filesystem_path):
                        os.remove(filesystem_path)
                except Exception as cleanup_error:
                    current_app.logger.warning(
                        f"清理失败图片文件失败: {filesystem_path}, error={cleanup_error}"
                    )


@products_v2_bp.route('/<model_number>', methods=['PUT'])
@cross_origin()
def update_product(model_number):
    """更新产品信息"""
    try:
        product = Product.query.get(model_number)
        if not product:
            return jsonify({'error': '产品不存在'}), 404

        # 获取更新数据
        product_data_str = request.form.get('product')
        if product_data_str:
            product_data = json.loads(product_data_str)

            # 更新字段（排除主键）
            for key, value in product_data.items():
                if key != 'model_number' and hasattr(product, key):
                    setattr(product, key, value)

        # 处理新上传的图片
        images = request.files.getlist('images')
        if images:
            # 获取当前最大排序号
            current_max_order = db.session.query(
                db.func.max(ProductImage.image_order)
            ).filter(ProductImage.model_number == model_number).scalar() or 0

            for idx, image_file in enumerate(images):
                if image_file and allowed_file(image_file.filename):
                    web_path, filesystem_path = save_product_image(image_file, model_number)

                    if web_path and filesystem_path:
                        try:
                            product_search_service = current_app.config.get('PRODUCT_SEARCH_SERVICE')
                            if product_search_service:
                                feature = product_search_service.extract_feature(filesystem_path)

                                product_image = ProductImage(
                                    model_number=model_number,
                                    image_path=web_path,
                                    vector=feature.tolist(),  # Pgvector expects a list, not bytes
                                    original_path=filesystem_path,
                                    image_order=current_max_order + idx + 1,
                                    is_primary=False
                                )
                                db.session.add(product_image)

                        except Exception as e:
                            current_app.logger.error(f"生成图片向量失败: {str(e)}")

        db.session.commit()

        # 无需刷新向量索引 (Stateless)
        # if images and current_app.config.get('PRODUCT_INDEX'):
        #     current_app.config['PRODUCT_INDEX'].refresh_from_database()

        return jsonify({'message': '产品更新成功'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"更新产品失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('/<model_number>', methods=['DELETE'])
@cross_origin()
def delete_product(model_number):
    """删除产品"""
    try:
        product = Product.query.get(model_number)
        if not product:
            return jsonify({'error': '产品不存在'}), 404

        db.session.delete(product)
        db.session.commit()

        # 无需刷新向量索引 (Stateless)
        # if current_app.config.get('PRODUCT_INDEX'):
        #     current_app.config['PRODUCT_INDEX'].refresh_from_database()

        return jsonify({'message': '产品删除成功'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"删除产品失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


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

        # 批量删除
        deleted_count = Product.query.filter(
            Product.model_number.in_(model_numbers)
        ).delete(synchronize_session=False)

        db.session.commit()

        # 无需刷新向量索引 (Stateless)
        # if deleted_count > 0 and current_app.config.get('PRODUCT_INDEX'):
        #     current_app.config['PRODUCT_INDEX'].refresh_from_database()

        return jsonify({
            'message': f'成功删除 {deleted_count} 个产品',
            'deleted_count': deleted_count
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"批量删除产品失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ========================================
# 图片管理 API
# ========================================

@products_v2_bp.route('/<model_number>/images/<int:image_id>', methods=['DELETE'])
@cross_origin()
def delete_product_image(model_number, image_id):
    """删除产品图片"""
    try:
        product_image = ProductImage.query.filter_by(
            id=image_id,
            model_number=model_number
        ).first()

        if not product_image:
            return jsonify({'error': '图片不存在'}), 404

        # 删除物理文件
        if product_image.original_path and os.path.exists(product_image.original_path):
            os.remove(product_image.original_path)

        db.session.delete(product_image)
        db.session.commit()

        # 无需刷新向量索引 (Stateless)
        # if current_app.config.get('PRODUCT_INDEX'):
        #     current_app.config['PRODUCT_INDEX'].refresh_from_database()

        return jsonify({'message': '图片删除成功'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"删除图片失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@products_v2_bp.route('/<model_number>/images/<int:image_id>/set-primary', methods=['POST'])
@cross_origin()
def set_primary_image(model_number, image_id):
    """设置主图"""
    try:
        # 取消当前主图
        ProductImage.query.filter_by(
            model_number=model_number,
            is_primary=True
        ).update({'is_primary': False})

        # 设置新主图
        product_image = ProductImage.query.filter_by(
            id=image_id,
            model_number=model_number
        ).first()

        if not product_image:
            return jsonify({'error': '图片不存在'}), 404

        product_image.is_primary = True
        db.session.commit()

        return jsonify({'message': '主图设置成功'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"设置主图失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


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
        csv_reader = csv.DictReader(io.StringIO(csv_content))

        # 统计信息
        stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }

        # 处理每一行
        for row_number, row in enumerate(csv_reader, start=2):  # 从第2行开始（第1行是表头）
            stats['total'] += 1

            try:
                # 验证必填字段
                required_fields = ['model_number', 'photographer_file', 'alibaba_product_url', 'category']
                for field in required_fields:
                    if not row.get(field) or str(row.get(field)).strip() == '':
                        raise ValueError(f'缺少必填字段: {field}')

                model_number = row['model_number'].strip()

                # 检查是否已存在
                if Product.query.get(model_number):
                    stats['skipped'] += 1
                    stats['errors'].append(f"第{row_number}行: 型号 {model_number} 已存在，跳过")
                    continue

                # 构建产品数据
                product_data = {
                    'model_number': model_number,
                    'photographer_file': row.get('photographer_file', '').strip(),
                    'alibaba_product_url': row.get('alibaba_product_url', '').strip(),
                    'category': row.get('category', '').strip(),
                }

                # 可选字段
                optional_fields = [
                    'spec_cn_reference', 'spec_cn', 'spec_en',
                    'product_size', 'package_size',
                    'price_1688', 'fob_price_tier1', 'fob_price_tier2', 'fob_price_tier3',
                    'intl_platform_price', 'competitor_price',
                    'ref_link_1', 'ref_link_2', 'ref_link_3',
                    'intl_platform_url', 'intl_platform_url_1', 'intl_platform_url_2'
                ]

                for field in optional_fields:
                    value = row.get(field, '').strip()
                    if value:
                        # 处理数字字段
                        if field.startswith('price_') or field.startswith('fob_') or field.endswith('_price'):
                            try:
                                product_data[field] = float(value)
                            except ValueError:
                                current_app.logger.warning(f"第{row_number}行: {field} 值无效: {value}")
                                product_data[field] = None
                        else:
                            product_data[field] = value

                # 创建产品
                product = Product.from_dict(product_data)
                db.session.add(product)
                db.session.commit()

                stats['success'] += 1

            except Exception as e:
                stats['failed'] += 1
                error_msg = f"第{row_number}行: {str(e)}"
                stats['errors'].append(error_msg)
                current_app.logger.error(error_msg)
                db.session.rollback()

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

        # 处理上传的图片
        if 'image' not in request.files:
            return error_response('缺少图片文件', 'MISSING_IMAGE_FILE', 400)

        image_file = request.files['image']
        if not image_file or not allowed_file(image_file.filename):
            return error_response('图片格式不支持', 'UNSUPPORTED_IMAGE_FORMAT', 400)

        # 保存临时文件
        temp_filename = f"search_{uuid.uuid4()}_{secure_filename(image_file.filename)}"
        temp_path = os.path.join(current_app.config['UPLOAD_FOLDER'], temp_filename)
        image_file.save(temp_path)

        try:
            # 执行搜索
            try:
                top_k = validate_top_k(request.form.get('top_k'))
            except ValueError as e:
                return error_response(str(e), 'INVALID_TOP_K', 400)

            results = search_service.search_similar_images(temp_path, top_k=top_k, request_id=request_id)

            # 搜索结果为空直接返回，避免无意义数据库查询
            if not results:
                return jsonify([])

            deduped_results = dedupe_results_by_model_number(results)

            # 获取产品详情
            model_numbers = [result.get('model_number') for result in deduped_results]
            products = Product.query.filter(Product.model_number.in_(model_numbers)).all()

            # 构建产品字典
            products_dict = {p.model_number: p for p in products}

            # 组装结果
            search_results = []
            for result in deduped_results:
                model_number = result.get('model_number')
                product = products_dict.get(model_number)

                if product:
                    product_data = product.to_dict()
                    product_data['similarity'] = result.get('similarity')
                    product_data['matched_image'] = result.get('image_path')
                    search_results.append(product_data)

            return jsonify(search_results)

        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)

    except EmbeddingServiceError as e:
        current_app.logger.error(f"图片搜索失败（向量服务） request_id={request_id}: {str(e)}")
        return error_response(str(e), 'EMBEDDING_SERVICE_ERROR', 503)
    except VectorSearchError as e:
        current_app.logger.error(f"图片搜索失败（向量检索） request_id={request_id}: {str(e)}")
        return error_response(str(e), 'VECTOR_SEARCH_ERROR', 500)
    except Exception as e:
        current_app.logger.error(f"图片搜索失败 request_id={request_id}: {str(e)}")
        return error_response(str(e), 'IMAGE_SEARCH_FAILED', 500)


# ========================================
# 统计 API
# ========================================

@products_v2_bp.route('/statistics', methods=['GET'])
@cross_origin()
def get_statistics():
    """获取产品统计信息"""
    try:
        total_products = Product.query.count()
        total_images = ProductImage.query.count()

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
