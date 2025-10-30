import os
from flask import Flask, send_from_directory, request, jsonify, abort
from flask_cors import CORS
from pathlib import Path
from models import db
from blueprints.customers import customers_bp
from blueprints.products import products_bp
from blueprints.orders import orders_bp
from blueprints.product_search import product_search_bp
from product_search import VectorProductIndex
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', 'zhang7481592630'),
    'database': os.getenv("DB_NAME", "xiangyipackage"),
    'charset': 'utf8mb4'
}
def create_app(config_name='development'):
    app = Flask(__name__)
    
    # 根据配置类型设置配置
    if config_name == 'testing':
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    else:
        # 使用统一的数据库配置
        app.config['SQLALCHEMY_DATABASE_URI'] = (
            f"mysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
            f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
            f"?charset={DB_CONFIG['charset']}"
        )
    
    # 配置CORS
    CORS(app, resources={
        r"/*": {
            "origins": [
                "http://localhost:5173", 
            ],
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
            "expose_headers": ["Content-Range", "X-Content-Range"],
            "supports_credentials": True,
            "max_age": 3600
        }
    }, supports_credentials=True)
    
    # 基础配置
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['DATASET_ROOT'] = os.getenv(
        'DATASET_ROOT',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '摄像师拍摄素材')
    )
    
    # 确保上传目录存在
    if not app.config['TESTING']:
        os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'size_images'), exist_ok=True)
        os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'good_images'), exist_ok=True)
    
    # 向量索引配置
    app.config['INDEX_PATH'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                          'data', 'product_search', 'product_index.bin')
    
    # 初始化扩展
    db.init_app(app)

    # 初始化向量索引
    if not app.config['TESTING']:
        # VectorProductIndex 初始化时会自动从数据库加载向量
        # 不再使用文件持久化，因为数据库是唯一的数据源
        product_index = VectorProductIndex()
        app.logger.info(f"向量索引已从数据库加载，共 {product_index.index.ntotal} 个向量")
        app.config['PRODUCT_INDEX'] = product_index
        
        # 确保向量索引目录存在
        Path(os.path.dirname(app.config['INDEX_PATH'])).mkdir(parents=True, exist_ok=True)
    
    # 注册蓝图
    app.register_blueprint(customers_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(product_search_bp)
    
    # 添加静态文件路由
    @app.route('/uploads/<path:filename>')
    def serve_upload(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

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
            db.session.execute('SELECT 1')
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
