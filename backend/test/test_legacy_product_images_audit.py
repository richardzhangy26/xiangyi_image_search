from services.legacy_product_images import audit_legacy_product_images


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _AuditConnection:
    def __init__(self, table_exists, row_count=None):
        self.table_exists = table_exists
        self.row_count = row_count
        self.statements = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if 'to_regclass' in sql:
            return _ScalarResult(self.table_exists)
        return _ScalarResult(self.row_count)


def test_audit_reports_absent_legacy_table():
    connection = _AuditConnection(table_exists=False)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is False
    assert audit.row_count is None
    assert audit.compatibility_required is False
    assert audit.required_actions == ()
    assert len(connection.statements) == 1


def test_audit_reports_empty_legacy_table():
    connection = _AuditConnection(table_exists=True, row_count=0)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is True
    assert audit.row_count == 0
    assert audit.compatibility_required is False
    assert len(connection.statements) == 2


def test_audit_requires_manual_migration_for_nonempty_legacy_table():
    connection = _AuditConnection(table_exists=True, row_count=1)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is True
    assert audit.row_count == 1
    assert audit.compatibility_required is True
    assert audit.required_actions == (
        '制定独立兼容迁移清单并取得明确授权',
        '在迁移完成前不得 DROP、DELETE 或转换 product_images',
    )
