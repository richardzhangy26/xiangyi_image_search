import React from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  List,
  Pagination,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ImageImportItem, ImageImportStatus } from '../types/product';


interface ImageImportTaskDrawerProps {
  open: boolean;
  onClose: () => void;
  items: ImageImportItem[];
  total: number;
  page: number;
  pageSize: number;
  unresolvedCount: number;
  processingCount: number;
  loading: boolean;
  error: string | null;
  onPageChange: (page: number) => void;
  onRefresh: () => void;
  onOpenAsset: (assetId: string) => void;
  onOpenRecycleBin?: () => void;
  onRetryItem?: (itemId: string) => void;
  retryingItemIds?: string[];
  selectedCancelIds?: string[];
  onCancelSelectionChange?: (ids: string[]) => void;
  onBulkCancel?: () => void;
  cancelling?: boolean;
}


const STATUS_PRESENTATION: Record<
  ImageImportStatus,
  { label: string; color: string }
> = {
  queued: { label: '等待中', color: 'default' },
  embedding: { label: '生成向量中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  awaiting_retry: { label: '等待重试', color: 'warning' },
  cancelled: { label: '已取消', color: 'default' },
};


// 与后端 CANCELABLE_STATUSES 对齐（Issue #20+#21 汇合终态）。
const CANCELABLE_STATUSES = new Set([
  'queued', 'embedding', 'failed', 'awaiting_retry',
]);


const ERROR_CLASS_LABELS: Record<string, string> = {
  rate_limited: '服务限流',
  network: '网络波动',
  server_error: '服务端暂时不可用',
  transient_storage: '预览读取暂时失败',
  storage_missing: '预览对象缺失',
  embedding_incompatible: '向量结果不可用',
  deterministic_request: '图片无法处理',
  unknown: '原因未分类',
};


const formatTime = (value: string | null) => {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false });
};


export const ImageImportTaskDrawer: React.FC<
  ImageImportTaskDrawerProps
> = ({
  open,
  onClose,
  items,
  total,
  page,
  pageSize,
  unresolvedCount,
  processingCount,
  loading,
  error,
  onPageChange,
  onRefresh,
  onOpenAsset,
  onOpenRecycleBin,
  onRetryItem,
  retryingItemIds = [],
  selectedCancelIds = [],
  onCancelSelectionChange,
  onBulkCancel,
  cancelling = false,
}) => (
  <Drawer
    title={(
      <div>
        <div>图片导入任务</div>
        <Typography.Text type="secondary" className="text-xs">
          未解决 {unresolvedCount} 项 · 处理中 {processingCount} 项
        </Typography.Text>
      </div>
    )}
    open={open}
    onClose={onClose}
    width={560}
    extra={(
      <Button icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>
        刷新
      </Button>
    )}
  >
    {error ? (
      <Alert type="error" showIcon message={error} className="mb-4" />
    ) : null}
    {onBulkCancel && selectedCancelIds.length > 0 ? (
      <div className="mb-4 flex items-center justify-between">
        <Typography.Text type="secondary" className="text-xs">
          已选 {selectedCancelIds.length} 项
        </Typography.Text>
        <Button danger loading={cancelling} onClick={onBulkCancel}>
          取消选中导入
        </Button>
      </div>
    ) : null}
    <Spin spinning={loading}>
      {items.length === 0 && !loading ? (
        <Empty description="暂无图片导入任务" />
      ) : (
        <List
          dataSource={items}
          renderItem={(item) => {
            const presentation = STATUS_PRESENTATION[item.status];
            const retryable = Boolean(
              onRetryItem
              && (item.status === 'failed' || item.status === 'awaiting_retry')
              && !item.cancel_requested_at
            );
            const cancelable = Boolean(
              onCancelSelectionChange
              && CANCELABLE_STATUSES.has(item.status)
              && !item.cancel_requested_at
            );
            const actions = [];
            if (item.recovery_action && onOpenRecycleBin) {
              actions.push(
                <Button key="recycle" type="link" onClick={onOpenRecycleBin}>
                  前往回收站
                </Button>
              );
            } else if (item.status === 'completed' && item.asset_id) {
              actions.push(
                <Button
                  key="asset"
                  type="link"
                  onClick={() => onOpenAsset(item.asset_id!)}
                >
                  查看待归款资产
                </Button>
              );
            }
            if (retryable) {
              actions.push(
                <Button
                  key="retry"
                  type="link"
                  loading={retryingItemIds.includes(item.item_id)}
                  onClick={() => onRetryItem!(item.item_id)}
                >
                  手工重试
                </Button>
              );
            }
            const errorLabel = item.last_error_class
              ? ERROR_CLASS_LABELS[item.last_error_class] ?? item.last_error_class
              : null;
            return (
              <List.Item actions={actions.length > 0 ? actions : undefined}>
                <List.Item.Meta
                  avatar={cancelable ? (
                    <Checkbox
                      aria-label={`选择取消 ${item.display_name}`}
                      checked={selectedCancelIds.includes(item.item_id)}
                      onChange={(event) => {
                        const next = event.target.checked
                          ? [...selectedCancelIds, item.item_id]
                          : selectedCancelIds.filter(
                            (id) => id !== item.item_id
                          );
                        onCancelSelectionChange!(next);
                      }}
                    />
                  ) : undefined}
                  title={(
                    <Space wrap>
                      <Typography.Text strong>{item.display_name}</Typography.Text>
                      <Tag color={presentation.color}>{presentation.label}</Tag>
                    </Space>
                  )}
                  description={(
                    <div className="space-y-1">
                      <Typography.Text type="secondary" className="block text-xs break-all">
                        {item.source_relative_path}
                      </Typography.Text>
                      <Typography.Text type="secondary" className="block text-xs">
                        创建：{formatTime(item.created_at)} · 更新：{formatTime(item.updated_at)}
                      </Typography.Text>
                      {item.status === 'awaiting_retry' ? (
                        <Typography.Text type="warning" className="block text-xs">
                          已尝试 {item.attempt_count}/{item.max_auto_attempts} 次
                          · 下次重试：{formatTime(item.next_retry_at)}
                          {errorLabel ? ` · 原因：${errorLabel}` : ''}
                        </Typography.Text>
                      ) : null}
                      {item.status === 'failed' && errorLabel ? (
                        <Typography.Text type="secondary" className="block text-xs">
                          失败原因：{errorLabel}
                          {' · '}已尝试 {item.attempt_count} 次
                        </Typography.Text>
                      ) : null}
                      {item.status === 'embedding' && item.cancel_requested_at ? (
                        <Typography.Text type="warning" className="block text-xs">
                          已请求取消，等待 worker 确认后不会形成正式资产
                        </Typography.Text>
                      ) : null}
                      {item.status === 'cancelled' && item.cancelled_at ? (
                        <Typography.Text type="secondary" className="block text-xs">
                          取消时间：{formatTime(item.cancelled_at)}
                        </Typography.Text>
                      ) : null}
                      {item.failure_message ? (
                        <Alert
                          type="error"
                          showIcon
                          message={item.failure_message}
                          className="mt-2"
                        />
                      ) : null}
                    </div>
                  )}
                />
              </List.Item>
            );
          }}
        />
      )}
    </Spin>
    {total > pageSize ? (
      <Pagination
        className="mt-4 text-right"
        current={page}
        pageSize={pageSize}
        total={total}
        showSizeChanger={false}
        onChange={onPageChange}
      />
    ) : null}
  </Drawer>
);
