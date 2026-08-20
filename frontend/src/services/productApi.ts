/**
 * 产品 API 服务层 - 电子产品配件
 * 对应后端 products_v2.py 蓝图
 */

import type {
  Product,
  ProductListResponse,
  ImageAssetSearchResult,
  ProductStatistics,
  CSVImportResponse,
  VectorIndexEvent,
  ProductFormData,
  ProductImageWriteSummary,
  ProductImageWriteResult,
  ImageAssetManagementItem,
  ImageAssetListResponse,
  ArchivedImageAssetListResponse,
  ImageAssetAssignmentResponse,
  ImageAssetImportResponse,
  ImageAssetArchiveResponse,
  ImageAssetRestoreResponse,
  ImageImportCreateResponse,
  ImageImportItem,
  ImageImportListResponse,
  ImageImportCancelBatchResponse,
} from '../types/product';
import { API_BASE_URL } from '../config';

export { API_BASE_URL };

export class ProductImageWriteError extends Error {
  status: number;
  errorCode?: string;
  imageResults: ProductImageWriteResult[];

  constructor(
    message: string,
    status: number,
    errorCode?: string,
    imageResults: ProductImageWriteResult[] = []
  ) {
    super(message);
    this.name = 'ProductImageWriteError';
    this.status = status;
    this.errorCode = errorCode;
    this.imageResults = imageResults;
  }
}

/** 同步本地导入的安全请求错误；仅网络和 5xx 可以由调用方重试。 */
export class ImageAssetImportRequestError extends Error {
  status: number | null;
  errorCode: string | null;
  retryable: boolean;

  constructor(message: string, status: number | null, errorCode: string | null) {
    super(message);
    this.name = 'ImageAssetImportRequestError';
    this.status = status;
    this.errorCode = errorCode;
    this.retryable = status === null || status >= 500;
  }
}

const readProductWriteResponse = async <T>(
  response: Response,
  fallbackError: string
): Promise<T> => {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ProductImageWriteError(
      payload.error || fallbackError,
      response.status,
      payload.error_code,
      Array.isArray(payload.image_results) ? payload.image_results : []
    );
  }
  return payload as T;
};

/**
 * 获取完整图片 URL
 */
export const getImageUrl = (imagePath: string): string => {
  if (imagePath.startsWith('http://') || imagePath.startsWith('https://')) {
    return imagePath;
  }

  if (!imagePath.startsWith('/')) {
    imagePath = '/' + imagePath;
  }

  return `${API_BASE_URL}${imagePath}`;
};

/**
 * 获取产品列表（支持分页和筛选）
 */
export const getProducts = async (params?: {
  page?: number;
  per_page?: number;
  category?: string;
  search?: string;
}): Promise<ProductListResponse> => {
  const queryParams = new URLSearchParams();

  if (params?.page !== undefined) queryParams.append('page', params.page.toString());
  if (params?.per_page !== undefined) queryParams.append('per_page', params.per_page.toString());
  if (params?.category) queryParams.append('category', params.category);
  if (params?.search) queryParams.append('search', params.search);

  const url = `${API_BASE_URL}/api/products${queryParams.toString() ? '?' + queryParams.toString() : ''}`;

  const response = await fetch(url, {
    method: 'GET',
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '获取产品列表失败' }));
    throw new Error(error.error || '获取产品列表失败');
  }

  return response.json();
};

/** 获取待归款图片资产。 */
export const getImageAssets = async (params: {
  assignment?: 'unassigned' | 'assigned' | 'all';
  page: number;
  perPage: number;
  search?: string;
}): Promise<ImageAssetListResponse> => {
  const query = new URLSearchParams({
    assignment: params.assignment || 'unassigned',
    page: String(params.page),
    per_page: String(params.perPage),
  });
  if (params.search) query.set('search', params.search);

  const response = await fetch(`${API_BASE_URL}/api/image-assets?${query}`, {
    method: 'GET',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({
      error: '获取待归款图片失败',
    }));
    throw new Error(error.error || '获取待归款图片失败');
  }
  return response.json();
};

/** 写入私有对象并创建持久导入项；服务端响应不等待 embedding。 */
export const createImageImports = async (
  files: File[]
): Promise<ImageImportCreateResponse> => {
  const body = new FormData();
  files.forEach((file) => body.append('images', file));
  const response = await fetch(`${API_BASE_URL}/api/image-imports`, {
    method: 'POST',
    body,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '图片导入排队失败'
    );
  }
  return payload as ImageImportCreateResponse;
};

/** 获取服务端持久导入项，用于刷新后恢复真实状态。 */
export const getImageImportItems = async (params: {
  page: number;
  perPage: number;
}): Promise<ImageImportListResponse> => {
  const query = new URLSearchParams({
    page: String(params.page),
    per_page: String(params.perPage),
  });
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports?${query}`,
    { method: 'GET' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '获取图片导入任务失败'
    );
  }
  return payload as ImageImportListResponse;
};

/** 获取一项持久导入状态。 */
export const getImageImportItem = async (
  itemId: string
): Promise<ImageImportItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports/${encodeURIComponent(itemId)}`,
    { method: 'GET' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '获取图片导入任务失败'
    );
  }
  return payload as ImageImportItem;
};

/** 手工重试一个失败或等待重试的持久导入项；幂等，不重新上传。 */
export const retryImageImportItem = async (
  itemId: string
): Promise<ImageImportItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports/${encodeURIComponent(itemId)}/retry`,
    { method: 'POST' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '手工重试失败'
    );
  }
  return payload as ImageImportItem;
};

/** 单项取消一个持久导入项；幂等，已完成项会被服务端拒绝。 */
export const cancelImageImportItem = async (
  itemId: string
): Promise<ImageImportItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports/${encodeURIComponent(itemId)}/cancel`,
    { method: 'POST' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '取消图片导入失败'
    );
  }
  return payload as ImageImportItem;
};

/** 批量取消持久导入项；返回逐项可理解结果。 */
export const cancelImageImportItems = async (
  itemIds: string[]
): Promise<ImageImportCancelBatchResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-imports/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_ids: itemIds }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '批量取消图片导入失败'
    );
  }
  return payload as ImageImportCancelBatchResponse;
};

/** 在保留窗口内恢复已取消的导入项（重新排队）。 */
export const restoreImageImportItem = async (
  itemId: string
): Promise<ImageImportItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports/${encodeURIComponent(itemId)}/restore`,
    { method: 'POST' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '恢复图片导入失败'
    );
  }
  return payload as ImageImportItem;
};

/** 提前放弃已取消或失败的导入项；不可逆，暂存对象将进入清理。 */
export const abandonImageImportItem = async (
  itemId: string
): Promise<ImageImportItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-imports/${encodeURIComponent(itemId)}/abandon`,
    { method: 'POST' }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === 'string'
        ? payload.error
        : '放弃图片导入失败'
    );
  }
  return payload as ImageImportItem;
};

/** 获取独立回收站中的图片资产。 */
export const getArchivedImageAssets = async (params: {
  page: number;
  perPage: number;
  search?: string;
}): Promise<ArchivedImageAssetListResponse> => {
  const query = new URLSearchParams({
    page: String(params.page),
    per_page: String(params.perPage),
  });
  if (params.search) query.set('search', params.search);

  const response = await fetch(
    `${API_BASE_URL}/api/image-assets/archived?${query}`,
    { method: 'GET' }
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({
      error: '获取回收站图片失败',
    }));
    throw new Error(error.error || '获取回收站图片失败');
  }
  return response.json();
};

export class ImageAssetRenameError extends Error {
  status: number;
  errorCode?: string;
  latest?: ImageAssetManagementItem;

  constructor(
    message: string,
    status: number,
    errorCode?: string,
    latest?: ImageAssetManagementItem
  ) {
    super(message);
    this.name = 'ImageAssetRenameError';
    this.status = status;
    this.errorCode = errorCode;
    this.latest = latest;
  }
}

/** 使用读取版本显式修改一项图片资产的显示名称主体。 */
export const renameImageAsset = async (
  assetId: string,
  nameBody: string,
  expectedVersion: number
): Promise<ImageAssetManagementItem> => {
  const response = await fetch(
    `${API_BASE_URL}/api/image-assets/${encodeURIComponent(assetId)}/rename`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name_body: nameBody,
        expected_version: expectedVersion,
      }),
    }
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ImageAssetRenameError(
      payload.error || '图片资产改名失败',
      response.status,
      payload.error_code,
      payload.latest
    );
  }
  return payload.asset;
};

/** 将图片资产事务化关联到一个型号；可选在型号不存在时快速创建产品。 */
export const assignImageAssets = async (
  assetIds: string[],
  modelNumber: string,
  options: { createIfMissing?: boolean } = {}
): Promise<ImageAssetAssignmentResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-assets/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      asset_ids: assetIds,
      model_number: modelNumber,
      create_if_missing: options.createIfMissing ?? false,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '关联型号失败' }));
    throw new Error(error.error || '关联型号失败');
  }
  return response.json();
};

/** 把本地图片批量导入为待归款图片资产（不创建产品记录）。 */
export const importImageAssets = async (
  files: File[],
  relativePaths: string[],
  prefix: string
): Promise<ImageAssetImportResponse> => {
  const formData = new FormData();
  files.forEach((file) => formData.append('images', file));
  formData.append('relative_paths', JSON.stringify(relativePaths));
  formData.append('prefix', prefix);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/image-assets/import`, {
      method: 'POST',
      body: formData,
    });
  } catch {
    throw new ImageAssetImportRequestError(
      '图片导入请求失败，请稍后重试',
      null,
      null
    );
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ImageAssetImportRequestError(
      typeof error.error === 'string' ? error.error : '图片导入失败',
      response.status,
      typeof error.error_code === 'string' ? error.error_code : null
    );
  }
  try {
    return await response.json();
  } catch (error) {
    if (
      error instanceof TypeError
      || (typeof error === 'object' && error !== null && error.name === 'AbortError')
    ) {
      throw new ImageAssetImportRequestError(
        '图片导入响应读取失败，请稍后重试',
        null,
        null
      );
    }
    throw new ImageAssetImportRequestError(
      '图片导入响应格式无效',
      response.status,
      null
    );
  }
};

/** 将未归款图片资产批量移入回收站。 */
export const archiveImageAssets = async (
  assetIds: string[]
): Promise<ImageAssetArchiveResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-assets/archive`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ asset_ids: assetIds }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || '图片移入回收站失败');
  }
  return payload;
};

/** 将回收站中的未归款图片资产批量恢复为 active。 */
export const restoreImageAssets = async (
  assetIds: string[]
): Promise<ImageAssetRestoreResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-assets/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ asset_ids: assetIds }),
  });
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const errorPayload = payload && typeof payload === 'object'
      ? payload as { error?: unknown; items?: unknown }
      : {};
    const itemErrors = Array.isArray(errorPayload.items)
      ? errorPayload.items
        .map((item: unknown) => {
          if (!item || typeof item !== 'object') return null;
          const itemError = (item as { error?: unknown }).error;
          return typeof itemError === 'string' && itemError.trim()
            ? itemError.trim()
            : null;
        })
        .filter((itemError): itemError is string => itemError !== null)
      : [];
    const topLevelError = typeof errorPayload.error === 'string'
      ? errorPayload.error
      : null;
    throw new Error(
      itemErrors.length > 0
        ? itemErrors.join('；')
        : topLevelError || '图片恢复失败'
    );
  }
  return payload as ImageAssetRestoreResponse;
};

/**
 * 获取单个产品详情
 */
export const getProductByModelNumber = async (modelNumber: string): Promise<Product> => {
  const response = await fetch(`${API_BASE_URL}/api/products/${encodeURIComponent(modelNumber)}`, {
    method: 'GET',
  });

  if (!response.ok) {
    let errorMessage = '获取产品详情失败';

    if (response.status === 404) {
      errorMessage = `产品型号 "${modelNumber}" 不存在`;
    } else {
      const error = await response.json().catch(() => ({}));
      errorMessage = error.error || errorMessage;
    }

    throw new Error(errorMessage);
  }

  return response.json();
};

/**
 * 创建新产品（支持图片上传）
 */
export const createProduct = async (
  productData: ProductFormData,
  images: File[]
): Promise<ProductImageWriteSummary & { message: string; model_number: string }> => {
  const formData = new FormData();
  formData.append('product', JSON.stringify(productData));

  images.forEach((image) => {
    formData.append('images', image);
  });

  const response = await fetch(`${API_BASE_URL}/api/products`, {
    method: 'POST',
    body: formData,
  });

  return readProductWriteResponse<
    ProductImageWriteSummary & { message: string; model_number: string }
  >(response, '创建产品失败');
};

/**
 * 更新产品信息
 */
export const updateProduct = async (
  modelNumber: string,
  productData: Partial<ProductFormData> & { image_order?: string[] },
  newImages?: File[]
): Promise<ProductImageWriteSummary & { message: string }> => {
  const formData = new FormData();
  formData.append('product', JSON.stringify(productData));

  if (newImages && newImages.length > 0) {
    newImages.forEach((image) => {
      formData.append('images', image);
    });
  }

  const response = await fetch(`${API_BASE_URL}/api/products/${encodeURIComponent(modelNumber)}`, {
    method: 'PUT',
    body: formData,
  });

  return readProductWriteResponse<
    ProductImageWriteSummary & { message: string }
  >(response, '更新产品失败');
};

/**
 * 删除产品
 */
export const deleteProduct = async (modelNumber: string): Promise<{ message: string }> => {
  const response = await fetch(`${API_BASE_URL}/api/products/${encodeURIComponent(modelNumber)}`, {
    method: 'DELETE',
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '删除产品失败' }));
    throw new Error(error.error || '删除产品失败');
  }

  return response.json();
};

/**
 * 批量删除产品
 */
export const batchDeleteProducts = async (
  modelNumbers: string[]
): Promise<{ message: string; deleted_count: number }> => {
  const response = await fetch(`${API_BASE_URL}/api/products/batch-delete`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ model_numbers: modelNumbers }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '批量删除失败' }));
    throw new Error(error.error || '批量删除失败');
  }

  return response.json();
};

/**
 * 删除产品图片
 */
export const deleteProductImage = async (
  modelNumber: string,
  assetId: string
): Promise<{ message: string }> => {
  const response = await fetch(
    `${API_BASE_URL}/api/products/${encodeURIComponent(modelNumber)}/images/${encodeURIComponent(assetId)}`,
    {
      method: 'DELETE',
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '删除图片失败' }));
    throw new Error(error.error || '删除图片失败');
  }

  return response.json();
};

/**
 * 设置主图
 */
export const setPrimaryImage = async (
  modelNumber: string,
  imageId: number
): Promise<{ message: string }> => {
  const response = await fetch(
    `${API_BASE_URL}/api/products/${encodeURIComponent(modelNumber)}/images/${imageId}/set-primary`,
    {
      method: 'POST',
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '设置主图失败' }));
    throw new Error(error.error || '设置主图失败');
  }

  return response.json();
};

/**
 * CSV 批量导入产品
 */
export const importProductsFromCSV = async (csvFile: File): Promise<CSVImportResponse> => {
  const formData = new FormData();
  formData.append('csv_file', csvFile);

  const response = await fetch(`${API_BASE_URL}/api/products/import-csv`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'CSV 导入失败' }));
    throw new Error(error.error || 'CSV 导入失败');
  }

  return response.json();
};

/**
 * 下载 CSV 导入模板
 */
export const downloadCSVTemplate = (): void => {
  window.open(`${API_BASE_URL}/api/products/csv-template`, '_blank');
};

/**
 * 构建向量索引（SSE 流式）
 */
export const buildVectorIndex = (
  onEvent: (event: VectorIndexEvent) => void,
  onError?: (error: string) => void
): (() => void) => {
  const eventSource = new EventSource(`${API_BASE_URL}/api/products/build-vector-index`);

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data) as VectorIndexEvent;
      onEvent(data);

      if (data.type === 'complete' || data.type === 'error') {
        eventSource.close();
      }
    } catch (error) {
      console.error('解析 SSE 事件失败:', error);
      onError?.('解析服务器响应失败');
      eventSource.close();
    }
  };

  eventSource.onerror = (err) => {
    console.error('EventSource 错误:', err);
    onError?.('连接服务器失败');
    eventSource.close();
  };

  // 返回清理函数
  return () => {
    eventSource.close();
  };
};

/**
 * 以图搜图
 */
export const searchProductsByImage = async (
  image: File,
  topK: number = 10
): Promise<ImageAssetSearchResult[]> => {
  const formData = new FormData();
  formData.append('image', image);
  formData.append('top_k', topK.toString());

  const response = await fetch(`${API_BASE_URL}/api/products/search`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    let errorMessage = '图片搜索失败';

    if (response.status === 413) {
      errorMessage = '图片过大，请缩小后重试';
    } else if (response.status === 400) {
      errorMessage = '图片格式不支持、文件损坏或无法安全解码';
    } else if (response.status === 503) {
      errorMessage = '图片识别服务暂不可用，请稍后重试';
    } else if (response.status === 500) {
      errorMessage = '服务器处理图片搜索时出错';
    } else {
      const error = await response.json().catch(() => ({}));
      errorMessage = error.error || errorMessage;
    }

    throw new Error(errorMessage);
  }

  return response.json();
};

/**
 * 获取产品统计信息
 */
export const getProductStatistics = async (): Promise<ProductStatistics> => {
  const response = await fetch(`${API_BASE_URL}/api/products/statistics`, {
    method: 'GET',
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '获取统计信息失败' }));
    throw new Error(error.error || '获取统计信息失败');
  }

  return response.json();
};
