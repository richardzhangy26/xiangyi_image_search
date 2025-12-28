/**
 * 产品图片搜索组件 - 电子产品配件
 */
import React, { useState, useRef, useCallback, useEffect } from 'react';
import { Card, Button, message, Spin, Empty, Descriptions, Image, Tag } from 'antd';
import { UploadOutlined, SearchOutlined, ClearOutlined } from '@ant-design/icons';
import type { ProductSearchResult } from '../types/product';
import { searchProductsByImage, getImageUrl } from '../services/productApi';

export const ProductSearch: React.FC = () => {
  const [searchImage, setSearchImage] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [results, setResults] = useState<ProductSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 统一的图片处理函数
  const handleImageFile = useCallback(
    (file: File, source: string) => {
      console.log(`处理图片文件 [${source}]:`, file.name);

      // 清理旧的预览URL
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }

      // 创建新的预览URL
      const newPreviewUrl = URL.createObjectURL(file);

      setSearchImage(file);
      setPreviewUrl(newPreviewUrl);
    },
    [previewUrl]
  );

  // 清理 URL
  useEffect(() => {
    return () => {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
    };
  }, [previewUrl]);

  // 粘贴事件监听
  useEffect(() => {
    const handlePaste = (e: ClipboardEvent) => {
      if (e.clipboardData && e.clipboardData.files.length > 0) {
        const file = e.clipboardData.files[0];
        if (file.type.startsWith('image/')) {
          e.preventDefault();
          handleImageFile(file, '粘贴');
          message.success('图片已从剪贴板粘贴');
        }
      }
    };

    document.addEventListener('paste', handlePaste);
    return () => {
      document.removeEventListener('paste', handlePaste);
    };
  }, [handleImageFile]);

  // 文件选择
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      handleImageFile(e.target.files[0], '文件选择');
    }
  };

  // 拖拽处理
  const handleDragEnter = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);

      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        const file = e.dataTransfer.files[0];
        if (file.type.startsWith('image/')) {
          handleImageFile(file, '拖拽');
          message.success('图片已上传');
        } else {
          message.error('请上传图片文件');
        }
      }
    },
    [handleImageFile]
  );

  // 搜索
  const handleSearch = async () => {
    if (!searchImage) {
      message.warning('请先上传搜索图片');
      return;
    }

    setLoading(true);
    try {
      const searchResults = await searchProductsByImage(searchImage, 10);
      setResults(searchResults);
      if (searchResults.length === 0) {
        message.info('未找到相似产品');
      } else {
        message.success(`找到 ${searchResults.length} 个相似产品`);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '搜索失败');
    } finally {
      setLoading(false);
    }
  };

  // 清除
  const handleClear = () => {
    setSearchImage(null);
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
    }
    setPreviewUrl(null);
    setResults([]);
  };

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold mb-6">以图搜款</h2>

      {/* 上传区域 */}
      <Card className="mb-6">
        <div
          className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
            isDragging ? 'border-blue-500 bg-blue-50' : 'border-gray-300'
          }`}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          {previewUrl ? (
            <div className="flex flex-col items-center space-y-4">
              <Image
                src={previewUrl}
                alt="搜索图片"
                style={{ maxHeight: 300, objectFit: 'contain' }}
              />
              <div className="flex space-x-2">
                <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch} loading={loading}>
                  搜索相似产品
                </Button>
                <Button icon={<ClearOutlined />} onClick={handleClear}>
                  清除
                </Button>
              </div>
            </div>
          ) : (
            <div>
              <UploadOutlined style={{ fontSize: 48, color: '#1890ff' }} />
              <p className="text-lg mt-4">拖拽图片到此处，或点击选择文件</p>
              <p className="text-gray-500 mt-2">支持 JPG、PNG、GIF 等格式</p>
              <p className="text-gray-500">提示: 您也可以直接粘贴剪贴板中的图片 (Ctrl+V / Cmd+V)</p>
              <Button
                type="primary"
                className="mt-4"
                onClick={() => fileInputRef.current?.click()}
              >
                选择文件
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                onChange={handleFileChange}
                style={{ display: 'none' }}
              />
            </div>
          )}
        </div>
      </Card>

      {/* 搜索结果 */}
      {loading ? (
        <div className="text-center py-12">
          <Spin size="large" tip="搜索中..." />
        </div>
      ) : results.length > 0 ? (
        <div>
          <h3 className="text-xl font-bold mb-4">搜索结果 ({results.length} 个)</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {results.map((result, index) => (
              <Card
                key={`${result.model_number}-${index}`}
                hoverable
                cover={
                  <div className="relative">
                    <Image
                      src={getImageUrl(result.matched_image || result.primary_image || '')}
                      alt={result.model_number}
                      style={{ height: 250, objectFit: 'cover' }}
                    />
                    <div className="absolute top-2 right-2">
                      <Tag color="blue">相似度: {((result.similarity || 0) * 100).toFixed(1)}%</Tag>
                    </div>
                  </div>
                }
              >
                <Card.Meta
                  title={result.model_number}
                  description={
                    <div>
                      <p className="text-gray-600 mb-2">{result.category}</p>
                      <Descriptions column={1} size="small">
                        <Descriptions.Item label="摄影师文件">
                          {result.photographer_file}
                        </Descriptions.Item>
                        {result.price_1688 && (
                          <Descriptions.Item label="1688价格">
                            ¥{result.price_1688.toFixed(2)}
                          </Descriptions.Item>
                        )}
                        {result.fob_price_tier1 && (
                          <Descriptions.Item label="FOB报价">
                            ${result.fob_price_tier1.toFixed(2)} - ${result.fob_price_tier3?.toFixed(2) || result.fob_price_tier1.toFixed(2)}
                          </Descriptions.Item>
                        )}
                        {result.spec_cn && (
                          <Descriptions.Item label="参数">
                            {result.spec_cn.substring(0, 50)}...
                          </Descriptions.Item>
                        )}
                      </Descriptions>
                      {result.alibaba_product_url && (
                        <a
                          href={result.alibaba_product_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-500 hover:text-blue-700 text-sm mt-2 inline-block"
                        >
                          查看阿里产品详情 →
                        </a>
                      )}
                    </div>
                  }
                />
              </Card>
            ))}
          </div>
        </div>
      ) : (
        !loading && searchImage && <Empty description="未找到相似产品" />
      )}
    </div>
  );
};
