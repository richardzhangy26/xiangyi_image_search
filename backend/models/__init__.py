from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from .product import Product, ProductImage
from .image_asset import ImageAsset

__all__ = ['db', 'Product', 'ProductImage', 'ImageAsset']
