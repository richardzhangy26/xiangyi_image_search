from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from .product import Product, ProductImage

__all__ = ['db', 'Product', 'ProductImage']
