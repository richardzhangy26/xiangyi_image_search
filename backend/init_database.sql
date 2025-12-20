-- ========================================
-- 电子产品配件图像搜索系统数据库初始化脚本
-- 数据库名: xiangyipackage_test
-- ========================================

-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS xiangyipackage_test
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE xiangyipackage_test;

-- ========================================
-- 1. products 表（电子产品配件主表）
-- ========================================
DROP TABLE IF EXISTS product_images;
DROP TABLE IF EXISTS products;

CREATE TABLE products (
    -- 主键和必填字段
    model_number VARCHAR(100) PRIMARY KEY COMMENT '型号（主键）',
    photographer_file VARCHAR(255) NOT NULL COMMENT '摄影师文件',
    alibaba_product_url VARCHAR(500) NOT NULL COMMENT '阿里产品链接',
    category VARCHAR(100) NOT NULL COMMENT '分类',

    -- 产品参数
    spec_cn_reference TEXT COMMENT '参数中文（参考）',
    spec_cn TEXT COMMENT '参数中文',
    spec_en TEXT COMMENT '参数英文',
    product_size VARCHAR(200) COMMENT '产品尺寸',
    package_size VARCHAR(200) COMMENT '包装尺寸',

    -- 价格相关（单位：美元）
    price_1688 DECIMAL(10, 2) COMMENT '1688价格',
    fob_price_tier1 DECIMAL(10, 2) COMMENT 'FOB报价 300-1999',
    fob_price_tier2 DECIMAL(10, 2) COMMENT 'FOB报价 2000-9999',
    fob_price_tier3 DECIMAL(10, 2) COMMENT 'FOB报价 >=10000',
    intl_platform_price DECIMAL(10, 2) COMMENT '国际站定价',
    competitor_price DECIMAL(10, 2) COMMENT '国际站同行定价',

    -- 参考链接
    ref_link_1 VARCHAR(500) COMMENT '链接1',
    ref_link_2 VARCHAR(500) COMMENT '链接2',
    ref_link_3 VARCHAR(500) COMMENT '链接3',
    intl_platform_url VARCHAR(500) COMMENT '国际站',
    intl_platform_url_1 VARCHAR(500) COMMENT '国际站1',
    intl_platform_url_2 VARCHAR(500) COMMENT '国际站2',

    -- 系统字段
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    -- 索引
    INDEX idx_category (category),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='电子产品配件表';

-- ========================================
-- 2. product_images 表（产品图片向量表）
-- ========================================
CREATE TABLE product_images (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '图片ID',
    model_number VARCHAR(100) NOT NULL COMMENT '关联产品型号',
    image_path VARCHAR(255) NOT NULL COMMENT 'Web访问路径',
    vector BLOB NOT NULL COMMENT '1024维图像向量',
    original_path TEXT COMMENT '文件系统原始路径',
    oss_path TEXT COMMENT 'OSS云存储路径（可选）',
    image_order INT DEFAULT 0 COMMENT '图片排序',
    is_primary BOOLEAN DEFAULT FALSE COMMENT '是否主图',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    -- 外键约束
    FOREIGN KEY (model_number) REFERENCES products(model_number) ON DELETE CASCADE,

    -- 唯一约束
    UNIQUE KEY unique_image_path (image_path),

    -- 索引
    INDEX idx_model_number (model_number),
    INDEX idx_is_primary (is_primary)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='产品图片向量表';

-- ========================================
-- 3. 插入测试数据（可选）
-- ========================================

-- 示例产品1：相机肩带
INSERT INTO products (
    model_number, photographer_file, alibaba_product_url, category,
    spec_cn, spec_en, product_size, package_size,
    price_1688, fob_price_tier1, fob_price_tier2, fob_price_tier3
) VALUES (
    'CS-001',
    'photographer_001',
    'https://detail.1688.com/offer/123456789.html',
    '相机肩带',
    '材质：纯棉，宽度：3.8cm，承重：5kg',
    'Material: Cotton, Width: 3.8cm, Load: 5kg',
    '长度120cm x 宽度3.8cm',
    '15cm x 8cm x 3cm',
    15.80,
    2.50,
    2.20,
    1.90
);

-- 示例产品2：相机挂绳
INSERT INTO products (
    model_number, photographer_file, alibaba_product_url, category,
    spec_cn, spec_en, product_size, package_size,
    price_1688, fob_price_tier1, fob_price_tier2, fob_price_tier3
) VALUES (
    'HL-002',
    'photographer_002',
    'https://detail.1688.com/offer/987654321.html',
    '相机挂绳',
    '材质：尼龙编织，宽度：1cm，承重：3kg',
    'Material: Nylon, Width: 1cm, Load: 3kg',
    '长度80cm x 宽度1cm',
    '12cm x 5cm x 2cm',
    8.50,
    1.20,
    1.00,
    0.85
);

-- ========================================
-- 4. 查看表结构
-- ========================================
SHOW TABLES;
DESCRIBE products;
DESCRIBE product_images;

-- ========================================
-- 5. 验证数据
-- ========================================
SELECT COUNT(*) as total_products FROM products;
SELECT * FROM products;

-- ========================================
-- 完成提示
-- ========================================
SELECT '数据库初始化完成！' AS status;
SELECT CONCAT('数据库: ', DATABASE()) AS current_database;
SELECT CONCAT('产品数量: ', COUNT(*)) AS product_count FROM products;
