import logging
import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, send_from_directory, jsonify, abort
from flask_cors import CORS
from pathlib import Path
from models import db
from blueprints.image_assets import image_assets_bp
from blueprints.products_v2 import products_v2_bp  # 新版本
from product_search import ImageSearchService
# 数据库配置（本地 PostgreSQL + pgvector）
# 优先使用 DATABASE_URL 完整连接串，否则由 DB_* 环境变量拼接
DATABASE_URL = os.getenv('DATABASE_URL')

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 5432)),
    'database': os.getenv("DB_NAME", "image_search"),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', ''),
}
def create_app(config_name='development'):
    # 配置结构化日志：services/ 下模块级 logger 继承 root logger 的默认级别
    # （WARNING），若不显式配置，logger.info(...)（如 vector.search.success）
    # 永远不会被输出。basicConfig 本身是幂等的——若 root 已有 handler 则
    # 不生效，这是当前唯一的日志配置点，不存在冲突。
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO'),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )

    app = Flask(__name__)

    # 根据配置类型设置配置
    if config_name == 'testing':
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    else:
        # 优先使用 DATABASE_URL，否则用 DB_* 配置拼接本地 PostgreSQL 连接
        if DATABASE_URL:
            app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
            app.logger.info("使用 DATABASE_URL 连接 PostgreSQL")
        else:
            app.config['SQLALCHEMY_DATABASE_URI'] = (
                f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
                f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
            )
            app.logger.info(f"使用本地 PostgreSQL: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    
    # 配置CORS（内网工具，允许任意来源：支持 Vite 开发端口、Docker 80 端口及局域网 IP 访问）
    CORS(app, resources={
        r"/*": {
            "origins": "*",
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
            "expose_headers": ["Content-Range", "X-Content-Range"],
            "max_age": 3600
        }
    })
    
    # 基础配置
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['OSS_SIGNED_URL_TTL_SECONDS'] = int(
        os.getenv('OSS_SIGNED_URL_TTL_SECONDS', '600')
    )
    app.config['DATASET_ROOT'] = os.getenv(
        'DATASET_ROOT',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '摄像师拍摄素材')
    )
    
    # 初始化扩展
    db.init_app(app)

    # 初始化向量搜索服务
    if not app.config['TESTING']:
        # ImageSearchService 是无状态的
        search_service = ImageSearchService()
        app.config['PRODUCT_SEARCH_SERVICE'] = search_service
        app.logger.info("ImageSearchService 初始化完成 (Stateless)")
    
    # 注册蓝图（使用新版本 products_v2）
    app.register_blueprint(products_v2_bp)
    app.register_blueprint(image_assets_bp)
    
    @app.route('/dataset-images/<path:filename>')
    def serve_dataset_image(filename):
        dataset_root = app.config.get('DATASET_ROOT')
        if not dataset_root or not os.path.isdir(dataset_root):
            abort(404)
        safe_root = os.path.realpath(dataset_root)
        requested_path = os.path.realpath(os.path.join(dataset_root, filename))
        if not requested_path.startswith(safe_root) or not os.path.isfile(requested_path):
            abort(404)
        directory, basename = os.path.split(requested_path)
        return send_from_directory(directory, basename)

    # 健康检查接口
    @app.route('/api/health', methods=['GET'])
    def health_check():
        """健康检查接口,用于 Docker 容器健康检查"""
        try:
            # 检查数据库连接
            from sqlalchemy import text
            db.session.execute(text('SELECT 1'))
            return jsonify({
                'status': 'healthy',
                'database': 'connected'
            }), 200
        except Exception as e:
            return jsonify({
                'status': 'unhealthy',
                'error': str(e)
            }), 503

    return app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000,debug=True)
