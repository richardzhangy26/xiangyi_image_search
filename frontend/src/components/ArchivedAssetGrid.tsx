import { useEffect, useRef, useState } from 'react';
import { ReloadOutlined, UndoOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Empty,
  Input,
  Pagination,
  Spin,
  Tag,
} from 'antd';
import type {
  ImageAssetManagementItem,
  PurgeBatchDto,
  PurgeConditionStatus,
  PurgeReadiness,
} from '../types/product';
import {
  POLLABLE_PURGE_BATCH_STATUSES,
  PURGE_BATCH_POLL_MS,
} from '../types/product';
import {
  cancelPurgeBatch,
  createPurgeBatch,
  getImageUrl,
  getPurgeBatch,
  getPurgeBatches,
  getPurgeReadiness,
  PurgeBatchRequestError,
  retryPurgeBatch,
} from '../services/productApi';

const ADMIN_TOKEN_KEY = 'xiangyi.adminPurgeToken';

const CONDITION_STATUS_LABELS: Record<PurgeConditionStatus, string> = {
  valid: '有效',
  failed: '失败',
  unknown: '未知',
  expired: '过期',
};

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
  const [adminOpen, setAdminOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState('');
  const [readiness, setReadiness] = useState<PurgeReadiness | null>(null);
  const [batches, setBatches] = useState<PurgeBatchDto[]>([]);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const idempotencyKeyRef = useRef<string | null>(null);

  useEffect(() => setDraftSearch(search), [search]);

  const sessionToken = (): string | null => sessionStorage.getItem(ADMIN_TOKEN_KEY);

  const loadReadiness = async (token: string) => {
    try {
      const result = await getPurgeReadiness(token);
      setReadiness(result);
    } catch {
      setReadiness(null);
    }
  };

  const loadBatches = async (token: string) => {
    try {
      const result = await getPurgeBatches(token);
      setBatches(result.batches);
    } catch {
      setBatches([]);
    }
  };

  useEffect(() => {
    if (!adminOpen) {
      return;
    }
    const stored = sessionToken();
    if (!stored) {
      return;
    }
    setTokenDraft(stored);
    void loadReadiness(stored);
    void loadBatches(stored);
  }, [adminOpen]);

  const pollable = batches.find((batch) => (
    POLLABLE_PURGE_BATCH_STATUSES as string[]
  ).includes(batch.status));

  useEffect(() => {
    if (!adminOpen || !pollable) {
      return;
    }
    const stored = sessionToken();
    if (!stored) {
      return;
    }
    const timer = window.setInterval(() => {
      void getPurgeBatch(pollable.batch_id, stored).then((latest) => {
        setBatches((current) => current.map((item) => (
          item.batch_id === latest.batch_id ? latest : item
        )));
      }).catch(() => undefined);
    }, PURGE_BATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [adminOpen, pollable?.batch_id, pollable?.status]);

  const saveToken = () => {
    const token = tokenDraft.trim();
    if (!token) {
      return;
    }
    sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
    void loadReadiness(token);
    void loadBatches(token);
  };

  const clearToken = () => {
    sessionStorage.removeItem(ADMIN_TOKEN_KEY);
    setTokenDraft('');
    setReadiness(null);
    setBatches([]);
    setBatchError(null);
    idempotencyKeyRef.current = null;
  };

  const expectedConfirmation = `永久删除 ${selectedAssetIds.length} 张`;

  const submitCreate = async () => {
    const token = sessionToken();
    if (!token) {
      return;
    }
    if (confirmation !== expectedConfirmation) {
      setBatchError(`请输入「${expectedConfirmation}」`);
      return;
    }
    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = crypto.randomUUID();
    }
    try {
      const created = await createPurgeBatch(
        selectedAssetIds,
        confirmation,
        idempotencyKeyRef.current,
        token,
      );
      setBatches((current) => [created, ...current.filter((item) => item.batch_id !== created.batch_id)]);
      setBatchError(null);
      idempotencyKeyRef.current = null;
    } catch (error) {
      setBatchError(error instanceof Error ? error.message : '创建清除批次失败');
    }
  };

  const submitCancel = async (batchId: string) => {
    const token = sessionToken();
    if (!token) {
      return;
    }
    try {
      const cancelled = await cancelPurgeBatch(batchId, token);
      setBatches((current) => current.map((item) => (
        item.batch_id === cancelled.batch_id ? cancelled : item
      )));
      setBatchError(null);
      idempotencyKeyRef.current = null;
    } catch (error) {
      if (error instanceof PurgeBatchRequestError && error.errorCode === 'PURGE_GATE_NOT_READY') {
        setBatchError(error.message || '安全门关闭时无法取消');
        return;
      }
      setBatchError(error instanceof Error ? error.message : '取消批次失败');
    }
  };

  const submitRetry = async (batchId: string) => {
    const token = sessionToken();
    if (!token) {
      return;
    }
    try {
      const retried = await retryPurgeBatch(batchId, token);
      setBatches((current) => current.map((item) => (
        item.batch_id === retried.batch_id ? retried : item
      )));
      setBatchError(null);
    } catch (error) {
      setBatchError(error instanceof Error ? error.message : '重试批次失败');
    }
  };

  return (
    <section className="asset-workbench" aria-label="回收站">
      <Collapse
        bordered={false}
        expandIcon={() => null}
        activeKey={adminOpen ? ['admin'] : []}
        onChange={(keys) => setAdminOpen(
          (Array.isArray(keys) ? keys : [keys]).includes('admin')
        )}
        items={[{
          key: 'admin',
          label: '管理员',
          children: adminOpen ? (
            <div>
              <Input.Password
                aria-label="管理员令牌"
                value={tokenDraft}
                onChange={(event) => setTokenDraft(event.target.value)}
              />
              <Button onClick={saveToken}>保存令牌</Button>
              <Button onClick={clearToken}>清除令牌</Button>
              {readiness && (
                <div>
                  {readiness.conditions.map((condition) => (
                    <div key={condition.id}>
                      {`${condition.label}：${CONDITION_STATUS_LABELS[condition.status]}`}
                    </div>
                  ))}
                  {readiness.purge_available
                    ? (readiness.pipeline_available
                      ? '安全门已满足，可创建永久清除批次'
                      : '安全门已满足，永久清除流水线尚未开放')
                    : '未满足安全门，永久清除不可用'}
                </div>
              )}
              {readiness?.pipeline_available && selectedAssetIds.length > 0 && selectedAssetIds.length <= 20 && (
                <div>
                  <Input
                    aria-label="永久删除确认"
                    value={confirmation}
                    onChange={(event) => {
                      setConfirmation(event.target.value);
                      idempotencyKeyRef.current = null;
                    }}
                    placeholder={expectedConfirmation}
                  />
                  <Button onClick={() => void submitCreate()}>创建批次</Button>
                </div>
              )}
              {batches.map((batch) => (
                <div key={batch.batch_id}>
                  <div>{`批次 ${batch.status}`}</div>
                  <div>{`已完成 ${batch.completed_count ?? 0} · 失败 ${batch.failed_count ?? 0} · 待处理 ${batch.pending_count ?? 0}`}</div>
                  {batch.cancellable === false && <div>批次已不可取消</div>}
                  {batch.error_code && <div>{batch.error_code}</div>}
                  {batch.items.map((item) => (
                    <div key={item.asset_id}>
                      {item.error_code || item.result_code || item.status}
                      {` · ${item.next_action}`}
                    </div>
                  ))}
                  {batch.cancellable !== false && (
                    <Button
                      aria-label="取消批次"
                      onClick={() => void submitCancel(batch.batch_id)}
                    >
                      取消批次
                    </Button>
                  )}
                  {batch.status === 'failed' && batch.error_code !== 'PURGE_BACKUP_RETENTION_EXPIRED' && (
                    <Button
                      aria-label="重试批次"
                      onClick={() => void submitRetry(batch.batch_id)}
                    >
                      重试批次
                    </Button>
                  )}
                </div>
              ))}
              <div>若恢复被拒绝，说明图片属于未取消的永久清除批次。</div>
            </div>
          ) : null,
        }]}
      />
      {adminOpen && batchError && (
        <div role="alert">{batchError}</div>
      )}
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
