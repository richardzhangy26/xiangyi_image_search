from dataclasses import dataclass

from sqlalchemy import text


@dataclass(frozen=True)
class LegacyProductImagesAudit:
    table_exists: bool
    row_count: int | None

    @property
    def compatibility_required(self) -> bool:
        return self.row_count not in (None, 0)

    @property
    def required_actions(self) -> tuple[str, ...]:
        if not self.compatibility_required:
            return ()
        return (
            '制定独立兼容迁移清单并取得明确授权',
            '在迁移完成前不得 DROP、DELETE 或转换 product_images',
        )

    def to_dict(self) -> dict[str, object]:
        return {
            'table': 'product_images',
            'table_exists': self.table_exists,
            'row_count': self.row_count,
            'compatibility_required': self.compatibility_required,
            'required_actions': list(self.required_actions),
        }


def audit_legacy_product_images(connection) -> LegacyProductImagesAudit:
    exists = connection.execute(
        text("SELECT to_regclass('public.product_images') IS NOT NULL")
    ).scalar_one()
    if not exists:
        return LegacyProductImagesAudit(False, None)
    row_count = connection.execute(
        text('SELECT COUNT(*) FROM product_images')
    ).scalar_one()
    return LegacyProductImagesAudit(True, int(row_count))
