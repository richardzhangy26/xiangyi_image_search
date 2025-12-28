from flask import Blueprint, request, jsonify, current_app
import os
import oss2
import uuid
from datetime import datetime
from typing import List, Optional
from werkzeug.utils import secure_filename

oss_bp = Blueprint('oss', __name__, url_prefix='/api/oss')

# 允许的文件类型
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def sanitize_oss_prefix(prefix: Optional[str], default: str = 'products') -> str:
    """清理OSS目标前缀，避免出现重复的/或..路径"""
    value = (prefix or default).strip()
    value = value.replace('\\', '/')
    parts = [segment for segment in value.split('/') if segment not in ('', '.', '..')]
    sanitized = '/'.join(parts)
    return sanitized or default

def build_public_url(bucket_name: str, endpoint: str, object_key: str) -> str:
    """根据Bucket和对象路径构建可访问URL"""
    endpoint = endpoint or ''
    if endpoint.startswith('http://'):
        endpoint = endpoint[7:]
    elif endpoint.startswith('https://'):
        endpoint = endpoint[8:]
    return f"https://{bucket_name}.{endpoint}/{object_key}"

def get_oss_client():
    """获取OSS客户端"""
    # 从环境变量获取OSS配置
    access_key_id = os.getenv('OSS_ACCESS_KEY_ID')
    access_key_secret = os.getenv('OSS_ACCESS_KEY_SECRET')
    endpoint = os.getenv('OSS_ENDPOINT')
    bucket_name = os.getenv('OSS_BUCKET_NAME')
    
    if not all([access_key_id, access_key_secret, endpoint, bucket_name]):
        raise ValueError("OSS配置不完整，请检查环境变量")
    
    # 创建Auth对象
    auth = oss2.Auth(access_key_id, access_key_secret)
    
    # 创建Bucket对象
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    
    return bucket, bucket_name

@oss_bp.route('/upload', methods=['POST'])
def upload_file():
    """上传文件到OSS"""
    try:
        # 检查是否有文件
        if 'file' not in request.files:
            return jsonify({'error': '没有文件'}), 400
        
        file = request.files['file']
        
        # 检查文件名
        if file.filename == '':
            return jsonify({'error': '没有选择文件'}), 400
        
        # 检查文件类型
        if not allowed_file(file.filename):
            return jsonify({'error': '不支持的文件类型'}), 400
        
        # 生成唯一文件名
        original_filename = secure_filename(file.filename)
        file_ext = original_filename.rsplit('.', 1)[1].lower()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        unique_id = str(uuid.uuid4().hex[:8])
        new_filename = f"{timestamp}_{unique_id}.{file_ext}"
        
        # 设置OSS存储路径
        folder = sanitize_oss_prefix(request.form.get('folder', 'products'))
        oss_path = f"{folder}/{new_filename}"
        
        # 获取OSS客户端
        bucket, bucket_name = get_oss_client()
        
        # 上传文件
        result = bucket.put_object(oss_path, file)
        
        # 生成访问URL
        endpoint = os.getenv('OSS_ENDPOINT', '')
        url = build_public_url(bucket_name, endpoint, oss_path)
        
        return jsonify({
            'message': '文件上传成功',
            'url': url,
            'path': oss_path,
            'filename': new_filename
        })
        
    except Exception as e:
        current_app.logger.error(f"上传文件到OSS时出错: {e}")
        return jsonify({'error': f'上传失败: {str(e)}'}), 500

@oss_bp.route('/delete', methods=['POST'])
def delete_file():
    """从OSS删除文件"""
    try:
        data = request.json
        oss_path = data.get('path')
        
        if not oss_path:
            return jsonify({'error': '未提供文件路径'}), 400
        
        # 获取OSS客户端
        bucket, _ = get_oss_client()
        
        # 删除文件
        bucket.delete_object(oss_path)
        
        return jsonify({'message': '文件删除成功'})
        
    except Exception as e:
        current_app.logger.error(f"从OSS删除文件时出错: {e}")
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

def _gather_files(base_dir: str) -> List[str]:
    """列出目录下所有允许的文件"""
    collected: List[str] = []
    for root, _, files in os.walk(base_dir):
        for filename in files:
            if allowed_file(filename):
                collected.append(os.path.join(root, filename))
    return collected

@oss_bp.route('/upload-folder', methods=['POST'])
def upload_folder():
    """批量上传本地目录中的图片到OSS，保留嵌套结构"""
    try:
        data = request.get_json(silent=True) or {}
        folder_path = data.get('folder_path')
        target_prefix = sanitize_oss_prefix(data.get('target_prefix', 'products'))
        overwrite = bool(data.get('overwrite', True))
        if not folder_path:
            return jsonify({'error': '请提供folder_path参数'}), 400
        abs_folder_path = os.path.abspath(folder_path)
        allowed_base = os.getenv('OSS_UPLOAD_BASE_PATH')
        if allowed_base:
            allowed_base = os.path.abspath(allowed_base)
            common = os.path.commonpath([allowed_base, abs_folder_path])
            if common != allowed_base:
                return jsonify({'error': f'folder_path必须位于允许的根目录内: {allowed_base}'}), 400
        if not os.path.exists(abs_folder_path):
            return jsonify({'error': f'路径不存在: {folder_path}'}), 400
        if not os.path.isdir(abs_folder_path):
            return jsonify({'error': f'路径不是文件夹: {folder_path}'}), 400

        files = _gather_files(abs_folder_path)
        if not files:
            return jsonify({'error': '文件夹中没有可上传的图片文件'}), 400

        bucket, bucket_name = get_oss_client()
        endpoint = os.getenv('OSS_ENDPOINT', '')
        uploaded: List[dict] = []
        skipped: List[dict] = []

        for file_path in files:
            relative_path = os.path.relpath(file_path, abs_folder_path)
            relative_path = relative_path.replace('\\', '/')
            object_key = f"{target_prefix}/{relative_path}"

            if not overwrite and bucket.object_exists(object_key):
                skipped.append({'local_path': file_path, 'oss_key': object_key, 'reason': '已存在'})
                continue

            with open(file_path, 'rb') as local_file:
                bucket.put_object(object_key, local_file)

            uploaded.append({
                'local_path': file_path,
                'oss_key': object_key,
                'url': build_public_url(bucket_name, endpoint, object_key)
            })

        return jsonify({
            'message': '批量上传完成',
            'total': len(files),
            'uploaded': len(uploaded),
            'skipped': len(skipped),
            'details': {
                'uploaded': uploaded,
                'skipped': skipped
            }
        })

    except Exception as e:
        current_app.logger.error(f"上传文件夹到OSS时出错: {e}")
        return jsonify({'error': f'上传文件夹失败: {str(e)}'}), 500
