from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from .product import Product
from .image_asset import ImageAsset

__all__ = ['db', 'Product', 'ImageAsset']
