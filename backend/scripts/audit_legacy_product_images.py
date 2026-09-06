#!/usr/bin/env python3
"""只读审计已退役的 ``product_images`` 表。"""

import json

from app import create_app
from models import db
from services.legacy_product_images import audit_legacy_product_images


def main():
    app = create_app()
    with app.app_context():
        audit = audit_legacy_product_images(db.session.connection())
    print(json.dumps(audit.to_dict(), ensure_ascii=False))


if __name__ == '__main__':
    main()
