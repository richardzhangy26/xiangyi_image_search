/**
 * 产品类型定义 - 电子产品配件
 * 对应后端 Product 模型
 */

/**
 * 产品完整信息
 */
export interface Product {
  // 主键和必填字段
  model_number: string;           // 型号（主键）
  photographer_file: string;      // 摄影师文件
  alibaba_product_url: string;    // 阿里产品链接
  category: string;               // 分类

  // 产品参数
  spec_cn_reference?: string;     // 参数中文（参考）
  spec_cn?: string;               // 参数中文
  spec_en?: string;               // 参数英文
  product_size?: string;          // 产品尺寸
  package_size?: string;          // 包装尺寸

  // 价格相关（单位：美元）
  price_1688?: number;            // 1688价格
  fob_price_tier1?: number;       // FOB报价 300-1999
  fob_price_tier2?: number;       // FOB报价 2000-9999
  fob_price_tier3?: number;       // FOB报价 >=10000
  intl_platform_price?: number;   // 国际站定价
  competitor_price?: number;      // 国际站同行定价

  // 参考链接
  ref_link_1?: string;            // 链接1
  ref_link_2?: string;            // 链接2
  ref_link_3?: string;            // 链接3
  intl_platform_url?: string;     // 国际站
  intl_platform_url_1?: string;   // 国际站1
  intl_platform_url_2?: string;   // 国际站2

  // 系统字段
  created_at?: string;            // 创建时间
  updated_at?: string;            // 更新时间

  // 扩展字段（前端需要）
  images?: ProductImage[];        // 产品图片列表
  primary_image?: string;         // 主图路径（便于显示）
}

/**
 * 产品图片信息
 */
export interface ProductImage {
  id: string;                     // 图片资产 UUID（兼容字段）
  asset_id: string;               // 图片资产 UUID
  model_number: string | null;    // 关联产品型号
  image_path: string;             // 站内私有预览路径（兼容字段）
  preview_url: string;            // 站内私有预览路径
  source_relative_path: string;   // 上传时的来源路径/文件名
  content_hash: string;           // 原图 SHA-256
  original_path: null;            // 正式图片不再使用本机路径
  image_order: number;            // 图片排序
  is_primary: boolean;            // 是否主图
  created_at?: string;            // 创建时间
}

export interface ProductImageWriteResult {
  asset_id: string;
  source_relative_path: string;
  status: 'created' | 'existing';
}

export interface ProductImageWriteSummary {
  uploaded_images: number;
  reused_images: number;
  skipped_duplicates: string[];
  image_results: ProductImageWriteResult[];
}

/**
 * 产品列表响应
 */
export interface ProductListResponse {
  products: Product[];
  total: number;
  page: number;
  per_page: number;
}

/** 产品管理页使用的安全图片资产摘要。 */
export interface ImageAssetManagementItem {
  asset_id: string;
  model_number: string | null;
  source_relative_path: string;
  preview_url: string;
  source_size: number;
  source_mime_type: string;
  source_width: number;
  source_height: number;
  created_at: string | null;
}

export interface ImageAssetListResponse {
  assets: ImageAssetManagementItem[];
  total: number;
  page: number;
  per_page: number;
}

export interface ImageAssetAssignmentResponse {
  model_number: string;
  assigned_count: number;
  reused_count: number;
  product_created: boolean;
}

/** 本地导入（单图/文件夹/剪贴板）单项结果。 */
export type ImageAssetImportItemStatus =
  | 'created'
  | 'existing'
  | 'skipped_duplicate_content'
  | 'source_conflict'
  | 'failed';

export interface ImageAssetImportItem {
  relative_path: string;
  status: ImageAssetImportItemStatus;
  asset_id: string | null;
  error: string | null;
}

export interface ImageAssetImportResponse {
  items: ImageAssetImportItem[];
  created_count: number;
  existing_count: number;
  skipped_count: number;
  failed_count: number;
}

/** 图片资产级搜索结果；商品型号允许尚未补充。 */
export interface ImageAssetSearchResult {
  asset_id: string;
  model_number: string | null;
  relative_path: string;
  preview_url: string;
  similarity: number;
}

/**
 * 产品统计信息
 */
export interface ProductStatistics {
  total_products: number;
  total_images: number;
  categories: Array<{
    name: string;
    count: number;
  }>;
}

/**
 * CSV 导入统计
 */
export interface CSVImportStats {
  total: number;
  success: number;
  failed: number;
  skipped: number;
  errors: string[];
}

/**
 * CSV 导入响应
 */
export interface CSVImportResponse {
  message: string;
  stats: CSVImportStats;
}

/**
 * 向量索引构建进度事件
 */
export type VectorIndexEvent =
  | { type: 'total'; value: number }
  | { type: 'progress'; processed: number; total: number; model_number: string; status: 'skipped' | 'no_images' | 'error' }
  | { type: 'complete'; message: string; processed: number; errors?: string[] }
  | { type: 'error'; message: string };

/**
 * 产品创建/更新表单数据
 */
export interface ProductFormData {
  model_number: string;
  photographer_file?: string;
  alibaba_product_url?: string;
  category?: string;
  spec_cn_reference?: string;
  spec_cn?: string;
  spec_en?: string;
  product_size?: string;
  package_size?: string;
  price_1688?: number;
  fob_price_tier1?: number;
  fob_price_tier2?: number;
  fob_price_tier3?: number;
  intl_platform_price?: number;
  competitor_price?: number;
  ref_link_1?: string;
  ref_link_2?: string;
  ref_link_3?: string;
  intl_platform_url?: string;
  intl_platform_url_1?: string;
  intl_platform_url_2?: string;
}
