"""
测试 Supabase 数据库连接
"""
import os
import sys
from pathlib import Path

# 添加父目录到 Python 路径
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv()

def test_direct_connection():
    """测试使用 DIRECT_URL 直连 Supabase"""
    print("\n========================================")
    print("测试 1: 使用 DIRECT_URL 直连")
    print("========================================")

    direct_url = os.getenv('DIRECT_URL')
    if not direct_url:
        print("❌ 错误: DIRECT_URL 未设置")
        return False

    # 隐藏密码显示连接信息
    masked_url = direct_url.split('@')[0].split(':')[:2] + ['***'] + ['@' + direct_url.split('@')[1]] if '@' in direct_url else []
    print(f"连接字符串: {':'.join(masked_url)}")

    try:
        import psycopg2
        from urllib.parse import urlparse

        # 解析连接 URL
        result = urlparse(direct_url)
        username = result.username
        password = result.password
        database = result.path[1:]
        hostname = result.hostname
        port = result.port

        print(f"主机: {hostname}")
        print(f"端口: {port}")
        print(f"数据库: {database}")
        print(f"用户名: {username}")

        # 尝试连接
        conn = psycopg2.connect(
            dbname=database,
            user=username,
            password=password,
            host=hostname,
            port=port
        )

        cursor = conn.cursor()
        cursor.execute('SELECT version()')
        version = cursor.fetchone()[0]
        print(f"\n✅ 连接成功!")
        print(f"PostgreSQL 版本: {version[:80]}...")

        cursor.close()
        conn.close()
        return True

    except Exception as e:
        print(f"\n❌ 连接失败: {e}")
        return False


def test_pooler_connection():
    """测试使用 DATABASE_URL 连接池"""
    print("\n========================================")
    print("测试 2: 使用 DATABASE_URL 连接池")
    print("========================================")

    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("❌ 错误: DATABASE_URL 未设置")
        return False

    # 隐藏密码显示连接信息
    masked_url = database_url.split('@')[0].split(':')[:2] + ['***'] + ['@' + database_url.split('@')[1]] if '@' in database_url else []
    print(f"连接字符串: {':'.join(masked_url)}")

    try:
        import psycopg2
        from urllib.parse import urlparse

        # 解析连接 URL
        result = urlparse(database_url)
        username = result.username
        password = result.password
        database = result.path[1:]
        hostname = result.hostname
        port = result.port

        print(f"主机: {hostname}")
        print(f"端口: {port}")
        print(f"数据库: {database}")
        print(f"用户名: {username}")

        # 尝试连接
        conn = psycopg2.connect(
            dbname=database,
            user=username,
            password=password,
            host=hostname,
            port=port
        )

        cursor = conn.cursor()
        cursor.execute('SELECT version()')
        version = cursor.fetchone()[0]
        print(f"\n✅ 连接成功!")
        print(f"PostgreSQL 版本: {version[:80]}...")

        cursor.close()
        conn.close()
        return True

    except Exception as e:
        print(f"\n❌ 连接失败: {e}")
        return False


def test_sqlalchemy_connection():
    """测试使用 SQLAlchemy 连接"""
    print("\n========================================")
    print("测试 3: 使用 SQLAlchemy ORM 连接")
    print("========================================")

    try:
        from app import create_app
        from models import db
        from sqlalchemy import text

        app = create_app()
        print(f"数据库 URI: {app.config['SQLALCHEMY_DATABASE_URI'].split('@')[0]}...@{app.config['SQLALCHEMY_DATABASE_URI'].split('@')[1] if '@' in app.config['SQLALCHEMY_DATABASE_URI'] else ''}")

        with app.app_context():
            # 测试基本查询
            result = db.session.execute(text('SELECT version()')).fetchone()
            print(f"\n✅ SQLAlchemy 连接成功!")
            print(f"PostgreSQL 版本: {result[0][:80]}...")

            # 检查表
            tables = db.session.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
            )).fetchall()
            print(f"\n📊 现有表 ({len(tables)} 个):")
            for table in tables:
                print(f"  - {table[0]}")

            # 检查 pgvector 扩展
            extensions = db.session.execute(text(
                "SELECT extname, extversion FROM pg_extension WHERE extname='vector'"
            )).fetchone()
            if extensions:
                print(f"\n🚀 pgvector 扩展: v{extensions[1]}")

            # 检查数据
            if 'products' in [t[0] for t in tables]:
                products_count = db.session.execute(text('SELECT COUNT(*) FROM products')).fetchone()
                print(f"\n📈 数据统计:")
                print(f"  - products 表: {products_count[0]} 条记录")

            if 'product_images' in [t[0] for t in tables]:
                images_count = db.session.execute(text('SELECT COUNT(*) FROM product_images')).fetchone()
                print(f"  - product_images 表: {images_count[0]} 条记录")

        return True

    except Exception as e:
        print(f"\n❌ SQLAlchemy 连接失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试"""
    print("\n" + "="*50)
    print("Supabase 数据库连接测试")
    print("="*50)

    # 检查环境变量
    print("\n📋 环境变量检查:")
    direct_url = os.getenv('DIRECT_URL')
    database_url = os.getenv('DATABASE_URL')

    print(f"  DIRECT_URL: {'✅ 已设置' if direct_url else '❌ 未设置'}")
    print(f"  DATABASE_URL: {'✅ 已设置' if database_url else '❌ 未设置'}")

    if not direct_url and not database_url:
        print("\n❌ 错误: 请在 .env 文件中设置 DIRECT_URL 或 DATABASE_URL")
        return

    # 运行测试
    results = []

    if direct_url:
        results.append(("DIRECT_URL 连接", test_direct_connection()))

    if database_url:
        results.append(("DATABASE_URL 连接", test_pooler_connection()))

    results.append(("SQLAlchemy 连接", test_sqlalchemy_connection()))

    # 汇总结果
    print("\n" + "="*50)
    print("测试结果汇总")
    print("="*50)
    for name, success in results:
        status = "✅ 成功" if success else "❌ 失败"
        print(f"{name}: {status}")

    print("\n")


if __name__ == '__main__':
    main()
