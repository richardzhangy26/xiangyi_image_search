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
} from '../types/product';
import { API_BASE_URL } from '../config';

export { API_BASE_URL };

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

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '创建产品失败' }));
    throw new Error(error.error || '创建产品失败');
  }

  return response.json();
};

/**
 * 更新产品信息
 */
export const updateProduct = async (
  modelNumber: string,
  productData: Partial<ProductFormData>,
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

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '更新产品失败' }));
    throw new Error(error.error || '更新产品失败');
  }

  return response.json();
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
