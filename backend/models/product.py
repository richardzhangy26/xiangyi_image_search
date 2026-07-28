from . import db
from datetime import datetime
from pgvector.sqlalchemy import Vector

class Product(db.Model):
    """电子产品配件模型（相机肩带、挂绳等）"""
    __tablename__ = 'products'

    # 主键和必填字段
    model_number = db.Column(db.String(100), primary_key=True, comment='型号（主键）')
    photographer_file = db.Column(db.String(255), nullable=False, comment='摄影师文件')
    alibaba_product_url = db.Column(db.String(500), nullable=False, comment='阿里产品链接')
    category = db.Column(db.String(100), nullable=False, comment='分类')

    # 产品参数
    spec_cn_reference = db.Column(db.Text, nullable=True, comment='参数中文（参考）')
    spec_cn = db.Column(db.Text, nullable=True, comment='参数中文')
    spec_en = db.Column(db.Text, nullable=True, comment='参数英文')
    product_size = db.Column(db.String(200), nullable=True, comment='产品尺寸')
    package_size = db.Column(db.String(200), nullable=True, comment='包装尺寸')

    # 价格相关（单位：美元）
    price_1688 = db.Column(db.Numeric(10, 2), nullable=True, comment='1688价格')
    fob_price_tier1 = db.Column(db.Numeric(10, 2), nullable=True, comment='FOB报价 300-1999')
    fob_price_tier2 = db.Column(db.Numeric(10, 2), nullable=True, comment='FOB报价 2000-9999')
    fob_price_tier3 = db.Column(db.Numeric(10, 2), nullable=True, comment='FOB报价 >=10000')
    intl_platform_price = db.Column(db.Numeric(10, 2), nullable=True, comment='国际站定价')
    competitor_price = db.Column(db.Numeric(10, 2), nullable=True, comment='国际站同行定价')

    # 参考链接
    ref_link_1 = db.Column(db.String(500), nullable=True, comment='链接1')
    ref_link_2 = db.Column(db.String(500), nullable=True, comment='链接2')
    ref_link_3 = db.Column(db.String(500), nullable=True, comment='链接3')
    intl_platform_url = db.Column(db.String(500), nullable=True, comment='国际站')
    intl_platform_url_1 = db.Column(db.String(500), nullable=True, comment='国际站1')
    intl_platform_url_2 = db.Column(db.String(500), nullable=True, comment='国际站2')

    # 系统字段
    created_at = db.Column(db.DateTime, default=datetime.now, comment='创建时间')
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    def __repr__(self):
        return f'<Product {self.model_number}>'

    def to_dict(self):
        """将产品信息转换为字典，用于API响应"""
        return {
            'model_number': self.model_number,
            'photographer_file': self.photographer_file,
            'alibaba_product_url': self.alibaba_product_url,
            'category': self.category,
            'spec_cn_reference': self.spec_cn_reference,
            'spec_cn': self.spec_cn,
            'spec_en': self.spec_en,
            'product_size': self.product_size,
            'package_size': self.package_size,
            'price_1688': float(self.price_1688) if self.price_1688 else None,
            'fob_price_tier1': float(self.fob_price_tier1) if self.fob_price_tier1 else None,
            'fob_price_tier2': float(self.fob_price_tier2) if self.fob_price_tier2 else None,
            'fob_price_tier3': float(self.fob_price_tier3) if self.fob_price_tier3 else None,
            'intl_platform_price': float(self.intl_platform_price) if self.intl_platform_price else None,
            'competitor_price': float(self.competitor_price) if self.competitor_price else None,
            'ref_link_1': self.ref_link_1,
            'ref_link_2': self.ref_link_2,
            'ref_link_3': self.ref_link_3,
            'intl_platform_url': self.intl_platform_url,
            'intl_platform_url_1': self.intl_platform_url_1,
            'intl_platform_url_2': self.intl_platform_url_2,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    @staticmethod
    def from_dict(data):
        """从字典创建产品对象"""
        product = Product()

        for key, value in data.items():
            if hasattr(product, key) and value is not None:
                # 过滤空字符串
                if isinstance(value, str) and value.strip() == '':
                    continue
                setattr(product, key, value)

        return product


class ProductImage(db.Model):
    """产品图片模型，存储产品图片路径和向量表示"""
    __tablename__ = 'product_images'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_number = db.Column(db.String(100), db.ForeignKey('products.model_number', ondelete='CASCADE'), nullable=False, comment='关联产品型号')
    image_path = db.Column(db.String(255), nullable=False, unique=True, comment='Web访问路径')
    vector = db.Column(Vector(1024), nullable=False, comment='1024维图像向量')
    content_hash = db.Column(db.String(64), nullable=True, comment='源文件 SHA-256（全库唯一，用于精确去重）')
    original_path = db.Column(db.Text, nullable=True, comment='文件系统原始路径')
    oss_path = db.Column(db.Text, nullable=True, comment='OSS云存储路径')
    image_order = db.Column(db.Integer, default=0, comment='图片排序')
    is_primary = db.Column(db.Boolean, default=False, comment='是否主图')
    created_at = db.Column(db.DateTime, default=datetime.now, comment='创建时间')

    # 建立与Product的关系
    product = db.relationship('Product', backref=db.backref('images', lazy=True, cascade='all, delete-orphan'))

    # 唯一索引名与 postgres/init/01_init.sql、init_db.py 保持一致
    __table_args__ = (
        db.Index('uq_product_images_content_hash', 'content_hash', unique=True),
    )

    def __repr__(self):
        return f'<ProductImage {self.id} for Product {self.model_number}>'

    def to_dict(self):
        """将图片信息转换为字典，用于API响应"""
        return {
            'id': self.id,
            'model_number': self.model_number,
            'image_path': self.image_path,
            'content_hash': self.content_hash,
            'original_path': self.original_path,
            'oss_path': self.oss_path,
            'image_order': self.image_order,
            'is_primary': self.is_primary,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    @staticmethod
    def from_dict(data):
        """从字典创建图片对象"""
        image = ProductImage()

        for key, value in data.items():
            if key == 'id':
                continue  # 跳过id字段，让数据库自动生成

            if hasattr(image, key) and value is not None:
                setattr(image, key, value)

        return image
