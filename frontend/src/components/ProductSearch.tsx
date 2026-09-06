/**
 * 产品图片搜索组件 - 电子产品配件
 */
import React, { useState, useRef, useCallback, useEffect } from 'react';
import { message, Spin, Empty } from 'antd';
import { Copy, Upload, Search, X, Sparkles, TrendingUp } from 'lucide-react';
import type { ImageAssetSearchResult } from '../types/product';
import { searchProductsByImage, getImageUrl } from '../services/productApi';
import { prepareSearchImage } from '../utils/prepareSearchImage';

export const ProductSearch: React.FC = () => {
  const [searchImage, setSearchImage] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [results, setResults] = useState<ImageAssetSearchResult[]>([]);
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
      const preparedImage = await prepareSearchImage(searchImage);
      if (preparedImage !== searchImage) {
        message.info('图片较大，已在浏览器中缩小后上传');
      }
      const searchResults = await searchProductsByImage(preparedImage, 10);
      setResults(searchResults);
      if (searchResults.length === 0) {
        message.info('未找到相似图片');
      } else {
        message.success(`找到 ${searchResults.length} 张相似图片`);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '搜索失败');
    } finally {
      setLoading(false);
    }
  };

  const copyRelativePath = async (relativePath: string) => {
    try {
      await navigator.clipboard.writeText(relativePath);
      message.success('相对路径已复制');
    } catch {
      message.error('复制失败，请手动选择路径');
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
    <div className="p-8">
      {/* 标题区域 */}
      <div className="mb-8">
        <div className="flex items-center space-x-3 mb-2">
          <div className="bg-gradient-to-br from-sky-500 to-sky-600 p-2 rounded-lg">
            <Search className="w-6 h-6 text-white" />
          </div>
          <h2 className="text-3xl font-bold text-slate-900">以图搜款</h2>
        </div>
        <p className="text-slate-600 ml-14">上传产品图片，AI 智能识别并匹配相似款式</p>
      </div>

      {/* 上传区域 */}
      <div className="mb-8">
        <div
          className={`
            relative border-2 border-dashed rounded-xl p-12 text-center
            transition-all duration-200
            ${
              isDragging
                ? 'border-sky-500 bg-sky-50 scale-[1.02]'
                : 'border-slate-300 bg-slate-50 hover:border-sky-400 hover:bg-slate-100'
            }
          `}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          {previewUrl ? (
            <div className="flex flex-col items-center space-y-6">
              <div className="relative group">
                <img
                  src={previewUrl}
                  alt="搜索图片"
                  className="max-h-80 rounded-lg shadow-lg object-contain"
                />
                <button
                  onClick={handleClear}
                  className="absolute top-2 right-2 bg-red-500 text-white p-2 rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-200 hover:bg-red-600"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex space-x-3">
                <button
                  onClick={handleSearch}
                  disabled={loading}
                  className="flex items-center space-x-2 px-6 py-3 bg-gradient-to-r from-sky-600 to-sky-700 text-white rounded-lg font-medium shadow-md hover:shadow-lg hover:from-sky-700 hover:to-sky-800 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loading ? (
                    <>
                      <Spin size="small" className="text-white" />
                      <span>搜索中...</span>
                    </>
                  ) : (
                    <>
                      <Sparkles className="w-5 h-5" />
                      <span>搜索相似产品</span>
                    </>
                  )}
                </button>
                <button
                  onClick={handleClear}
                  className="flex items-center space-x-2 px-6 py-3 bg-slate-200 text-slate-700 rounded-lg font-medium hover:bg-slate-300 transition-colors duration-200"
                >
                  <X className="w-5 h-5" />
                  <span>清除</span>
                </button>
              </div>
            </div>
          ) : (
            <div>
              <div className="flex justify-center mb-4">
                <div className="bg-gradient-to-br from-sky-100 to-sky-200 p-6 rounded-full">
                  <Upload className="w-12 h-12 text-sky-600" />
                </div>
              </div>
              <h3 className="text-xl font-semibold text-slate-900 mb-2">
                拖拽图片到此处，或点击选择文件
              </h3>
              <p className="text-slate-600 mb-1">支持 JPG、PNG、GIF、WebP 等格式</p>
              <p className="text-sm text-slate-500 mb-6">
                💡 提示: 您也可以直接粘贴剪贴板中的图片 (Ctrl+V / Cmd+V)
              </p>
              <button
                onClick={() => fileInputRef.current?.click()}
                className="inline-flex items-center space-x-2 px-6 py-3 bg-sky-600 text-white rounded-lg font-medium shadow-md hover:bg-sky-700 hover:shadow-lg transition-all duration-200"
              >
                <Upload className="w-5 h-5" />
                <span>选择文件</span>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                onChange={handleFileChange}
                className="hidden"
              />
            </div>
          )}
        </div>
      </div>

      {/* 搜索结果 */}
      {loading ? (
        <div className="text-center py-20">
          <Spin size="large" />
          <p className="mt-4 text-slate-600 text-lg">AI 正在分析图片并搜索相似图片...</p>
        </div>
      ) : results.length > 0 ? (
        <div>
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center space-x-3">
              <TrendingUp className="w-6 h-6 text-sky-600" />
              <h3 className="text-2xl font-bold text-slate-900">
                搜索结果
                <span className="ml-2 text-lg text-slate-500">({results.length} 张相似图片)</span>
              </h3>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {results.map((result) => (
              <div
                key={result.asset_id}
                className="group bg-white rounded-xl border border-slate-200 overflow-hidden hover:shadow-xl hover:border-sky-300 transition-all duration-200"
              >
                <div className="relative overflow-hidden">
                  <img
                    src={getImageUrl(result.preview_url)}
                    alt={result.display_name}
                    className="w-full h-64 object-cover group-hover:scale-105 transition-transform duration-300"
                  />
                  <div className="absolute top-3 right-3">
                    <div className="bg-gradient-to-r from-sky-600 to-sky-700 text-white px-3 py-1.5 rounded-full text-sm font-semibold shadow-lg">
                      相似度: {((result.similarity || 0) * 100).toFixed(1)}%
                    </div>
                  </div>
                </div>
                <div className="p-5">
                  <h4 className="text-lg font-bold text-slate-900 mb-3">
                    {result.display_name}
                  </h4>
                  <p className="text-sm text-slate-500 mb-3">
                    型号：{result.model_number || '未归款'}
                  </p>
                  <div className="rounded-lg bg-slate-50 border border-slate-100 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <p className="text-sm text-slate-700 break-all whitespace-normal m-0">
                        {result.source_relative_path}
                      </p>
                      <button
                        type="button"
                        onClick={() => copyRelativePath(result.source_relative_path)}
                        className="shrink-0 inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:border-sky-300 hover:text-sky-700"
                        aria-label={`复制相对路径 ${result.source_relative_path}`}
                      >
                        <Copy className="w-3.5 h-3.5" />
                        复制相对路径
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : (
        !loading &&
        searchImage && (
          <div className="text-center py-20">
            <Empty description="未找到相似图片" />
          </div>
        )
      )}
    </div>
  );
};
