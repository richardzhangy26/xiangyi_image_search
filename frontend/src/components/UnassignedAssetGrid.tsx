import { useEffect, useState } from 'react';
import { DeleteOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  Pagination,
  Segmented,
  Spin,
  Tag,
  Tooltip,
} from 'antd';
import type { ImageAssetManagementItem } from '../types/product';
import { getImageUrl } from '../services/productApi';
import { AssetDisplayNameEditor } from './AssetDisplayNameEditor';

export type AssetAssignment = 'unassigned' | 'assigned' | 'all';

export interface UnassignedAssetGridProps {
  assets: ImageAssetManagementItem[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  error: string | null;
  search: string;
  assignment: AssetAssignment;
  selectedAssetIds: string[];
  canAssign: boolean;
  onSearch: (value: string) => void;
  onAssignmentChange: (value: AssetAssignment) => void;
  onPageChange: (page: number) => void;
  onSelectionChange: (assetIds: string[]) => void;
  onAssign: () => void;
  onArchive: () => void;
  onRetry: () => void;
  onAssetRenamed: (asset: ImageAssetManagementItem) => void;
}

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
};

const toggleSelection = (
  current: string[],
  assetId: string,
  checked: boolean
): string[] => checked
  ? [...current, assetId]
  : current.filter((id) => id !== assetId);

export function UnassignedAssetGrid({
  assets,
  total,
  page,
  pageSize,
  loading,
  error,
  search,
  assignment,
  selectedAssetIds,
  canAssign,
  onSearch,
  onAssignmentChange,
  onPageChange,
  onSelectionChange,
  onAssign,
  onArchive,
  onRetry,
  onAssetRenamed,
}: UnassignedAssetGridProps) {
  const [draftSearch, setDraftSearch] = useState(search);

  useEffect(() => setDraftSearch(search), [search]);

  return (
    <section className="asset-workbench" aria-label="图片资产">
      <div className="asset-toolbar">
        <div className="asset-search-wrap">
          <Segmented
            value={assignment}
            options={[
              { label: '待归款', value: 'unassigned' },
              { label: '已归款', value: 'assigned' },
              { label: '全部', value: 'all' },
            ]}
            onChange={(value) => onAssignmentChange(value as AssetAssignment)}
          />
          <Input.Search
            placeholder="搜索显示名称或来源路径"
            value={draftSearch}
            allowClear
            onChange={(event) => setDraftSearch(event.target.value)}
            onSearch={(value) => onSearch(value.trim())}
          />
          <span className="asset-search-hint">
            同时匹配业务显示名称和不可变来源路径
          </span>
        </div>

        {selectedAssetIds.length > 0 && <div className="asset-batch-actions">
          <span className="asset-selection-count">
            已选 {selectedAssetIds.length} 张
          </span>
          <Tooltip
            title={canAssign ? undefined : '请先在产品视图中添加或导入真实型号'}
          >
            <span>
              <Button
                type="primary"
                icon={<LinkOutlined />}
                aria-label="关联型号"
                disabled={selectedAssetIds.length === 0 || !canAssign}
                onClick={onAssign}
              >
                关联型号
              </Button>
            </span>
          </Tooltip>
          <Button
            danger
            icon={<DeleteOutlined />}
            aria-label="移入回收站"
            onClick={onArchive}
          >
            移入回收站
          </Button>
        </div>
        }
      </div>

      {!canAssign && (
        <div className="asset-guidance">
          当前还没有产品型号。你仍可浏览全部图片；需要归款时，请先切换到“产品资料”添加或 CSV 导入真实型号。
        </div>
      )}

      {error && (
        <Alert
          type="error"
          showIcon
          message="图片资产加载失败"
          description={error}
          action={(
            <Button icon={<ReloadOutlined />} onClick={onRetry}>
              重试
            </Button>
          )}
        />
      )}

      <Spin spinning={loading} tip="正在整理图片资产…">
        {!error && !loading && assets.length === 0 ? (
          <div className="asset-empty">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={search ? '没有匹配显示名称或来源路径的图片' : '暂无图片资产'}
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
                      disabled={isAssigned}
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
                    <AssetDisplayNameEditor
                      asset={asset}
                      onRenamed={onAssetRenamed}
                    />
                    <div
                      className="asset-path"
                      title={asset.source_relative_path}
                    >
                      {asset.source_relative_path}
                    </div>
                    {isAssigned && (
                      <Tag bordered={false} color="cyan">
                        已归款 · {asset.model_number}
                      </Tag>
                    )}
                    <div className="asset-meta">
                      <span>{asset.source_width} × {asset.source_height}</span>
                      <span>{formatBytes(asset.source_size)}</span>
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
