import { useEffect, useState } from 'react';
import { ReloadOutlined, UndoOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  Pagination,
  Spin,
  Tag,
} from 'antd';
import type { ImageAssetManagementItem } from '../types/product';
import { getImageUrl } from '../services/productApi';

export interface ArchivedAssetGridProps {
  assets: ImageAssetManagementItem[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  error: string | null;
  search: string;
  selectedAssetIds: string[];
  restoring: boolean;
  onSearch: (value: string) => void;
  onPageChange: (page: number) => void;
  onSelectionChange: (assetIds: string[]) => void;
  onRestore: () => void;
  onRetry: () => void;
}

const toggleSelection = (
  current: string[],
  assetId: string,
  checked: boolean
): string[] => checked
  ? current.includes(assetId) ? current : [...current, assetId]
  : current.filter((id) => id !== assetId);

const formatArchivedAt = (value: string | null): string => {
  if (!value) return '未知';
  return value.replace('T', ' ').replace(/\.\d+$/, '').slice(0, 16);
};

export function ArchivedAssetGrid({
  assets,
  total,
  page,
  pageSize,
  loading,
  error,
  search,
  selectedAssetIds,
  restoring,
  onSearch,
  onPageChange,
  onSelectionChange,
  onRestore,
  onRetry,
}: ArchivedAssetGridProps) {
  const [draftSearch, setDraftSearch] = useState(search);

  useEffect(() => setDraftSearch(search), [search]);

  return (
    <section className="asset-workbench" aria-label="回收站">
      <div className="asset-toolbar">
        <div className="asset-search-wrap">
          <Input.Search
            placeholder="搜索显示名称或来源路径"
            value={draftSearch}
            allowClear
            disabled={restoring}
            onChange={(event) => setDraftSearch(event.target.value)}
            onSearch={(value) => onSearch(value.trim())}
          />
          <span className="asset-search-hint">
            同时匹配显示名称和来源路径
          </span>
        </div>

        {selectedAssetIds.length > 0 && (
          <div className="asset-batch-actions">
            <span className="asset-selection-count">
              已选 {selectedAssetIds.length} 张
            </span>
            <Button
              type="primary"
              icon={<UndoOutlined />}
              aria-label="恢复选中图片"
              loading={restoring}
              disabled={restoring}
              onClick={onRestore}
            >
              恢复选中图片
            </Button>
          </div>
        )}
      </div>

      <div className="asset-guidance">
        回收站保留图片资产的原有身份和私有预览；只有未归款图片可以批量恢复。
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message="回收站加载失败"
          description={error}
          action={(
            <Button
              icon={<ReloadOutlined />}
              disabled={restoring}
              onClick={onRetry}
            >
              重试
            </Button>
          )}
        />
      )}

      <Spin spinning={loading} tip="正在整理回收站…">
        {!error && !loading && assets.length === 0 ? (
          <div className="asset-empty">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={search
                ? '没有匹配显示名称或来源路径的归档图片'
                : '回收站暂无图片'}
            />
          </div>
        ) : (
          <div className="asset-card-grid">
            {assets.map((asset) => {
              const selected = selectedAssetIds.includes(asset.asset_id);
              const isAssigned = asset.model_number !== null;
              return (
                <article
                  key={asset.asset_id}
                  className={`asset-card${selected ? ' asset-card-selected' : ''}`}
                >
                  <div className="asset-card-check">
                    <Checkbox
                      aria-label={`选择 ${asset.display_name}`}
                      checked={selected}
                      disabled={isAssigned || restoring}
                      onChange={(event) => onSelectionChange(toggleSelection(
                        selectedAssetIds,
                        asset.asset_id,
                        event.target.checked
                      ))}
                    />
                  </div>
                  <div className="asset-preview-frame">
                    <img
                      src={getImageUrl(asset.preview_url)}
                      alt={asset.display_name}
                      loading="lazy"
                    />
                  </div>
                  <div className="asset-card-body">
                    <div
                      className="asset-display-name"
                      title={asset.display_name}
                    >
                      {asset.display_name}
                    </div>
                    <div
                      className="asset-path"
                      title={asset.source_relative_path}
                    >
                      {asset.source_relative_path}
                    </div>
                    {isAssigned && (
                      <Tag bordered={false} color="orange">
                        已归款 · {asset.model_number} · 不可恢复
                      </Tag>
                    )}
                    <div className="asset-meta">
                      <span>
                        归档时间：{formatArchivedAt(asset.archived_at)}
                      </span>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Spin>

      {total > 0 && (
        <div className="asset-pagination">
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            disabled={restoring}
            showSizeChanger={false}
            showQuickJumper
            showTotal={(value) => `共 ${value} 张`}
            onChange={(nextPage) => onPageChange(nextPage)}
          />
        </div>
      )}
    </section>
  );
}
