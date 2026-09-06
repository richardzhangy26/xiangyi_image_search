import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ImageImportItem } from '../types/product';
import { ImageImportTaskDrawer } from './ImageImportTaskDrawer';


const items: ImageImportItem[] = [
  {
    item_id: 'queued-19', display_name: '等待图片.png',
    source_relative_path: 'imports/a/0001/等待图片.png', source_revision: 1,
    status: 'queued', asset_id: null, failure_message: null,
    attempt_count: 0, max_auto_attempts: 5, last_error_class: null,
    last_attempt_at: null, next_retry_at: null,
    cancel_requested_at: null, cancelled_at: null,
    purge_eligible_at: null, objects_purged_at: null,
    created_at: '2026-08-09T12:00:00', updated_at: '2026-08-09T12:00:00',
    embedding_started_at: null, completed_at: null, failed_at: null,
  },
  {
    item_id: 'embedding-19', display_name: '识别图片.png',
    source_relative_path: 'imports/b/0001/识别图片.png', source_revision: 1,
    status: 'embedding', asset_id: null, failure_message: null,
    attempt_count: 1, max_auto_attempts: 5, last_error_class: null,
    last_attempt_at: '2026-08-09T12:02:00', next_retry_at: null,
    cancel_requested_at: null, cancelled_at: null,
    purge_eligible_at: null, objects_purged_at: null,
    created_at: '2026-08-09T12:01:00', updated_at: '2026-08-09T12:02:00',
    embedding_started_at: '2026-08-09T12:02:00', completed_at: null,
    failed_at: null,
  },
  {
    item_id: 'completed-19', display_name: '完成图片.png',
    source_relative_path: 'imports/c/0001/完成图片.png', source_revision: 1,
    status: 'completed', asset_id: 'asset-19', failure_message: null,
    attempt_count: 1, max_auto_attempts: 5, last_error_class: null,
    last_attempt_at: '2026-08-09T12:03:10', next_retry_at: null,
    cancel_requested_at: null, cancelled_at: null,
    purge_eligible_at: null, objects_purged_at: null,
    created_at: '2026-08-09T12:03:00', updated_at: '2026-08-09T12:04:00',
    embedding_started_at: '2026-08-09T12:03:10',
    completed_at: '2026-08-09T12:04:00', failed_at: null,
  },
  {
    item_id: 'failed-19', display_name: '失败图片.png',
    source_relative_path: 'imports/d/0001/失败图片.png', source_revision: 1,
    status: 'failed', asset_id: null,
    failure_message: '处理失败（InvalidEmbeddingResult）',
    attempt_count: 5, max_auto_attempts: 5,
    last_error_class: 'embedding_incompatible',
    last_attempt_at: '2026-08-09T12:05:10', next_retry_at: null,
    cancel_requested_at: null, cancelled_at: null,
    purge_eligible_at: null, objects_purged_at: null,
    created_at: '2026-08-09T12:05:00', updated_at: '2026-08-09T12:06:00',
    embedding_started_at: '2026-08-09T12:05:10', completed_at: null,
    failed_at: '2026-08-09T12:06:00',
  },
];


const awaitingRetryItem: ImageImportItem = {
  item_id: 'retry-20', display_name: '重试图片.png',
  source_relative_path: 'imports/e/0001/重试图片.png', source_revision: 1,
  status: 'awaiting_retry', asset_id: null,
  failure_message: '处理失败（EmbeddingRateLimitExhaustedError）',
  attempt_count: 2, max_auto_attempts: 5, last_error_class: 'rate_limited',
  last_attempt_at: '2026-08-10T12:00:00',
  next_retry_at: '2026-08-10T12:01:00',
  cancel_requested_at: null, cancelled_at: null,
  purge_eligible_at: null, objects_purged_at: null,
  created_at: '2026-08-10T11:59:00', updated_at: '2026-08-10T12:00:00',
  embedding_started_at: '2026-08-10T11:59:30', completed_at: null,
  failed_at: null,
};


const cancelledItem: ImageImportItem = {
  item_id: 'cancelled-21', display_name: '取消图片.png',
  source_relative_path: 'imports/f/0001/取消图片.png', source_revision: 1,
  status: 'cancelled', asset_id: null, failure_message: null,
  attempt_count: 0, max_auto_attempts: 5, last_error_class: null,
  last_attempt_at: null, next_retry_at: null,
  cancel_requested_at: '2026-08-10T12:00:00',
  cancelled_at: '2026-08-10T12:00:05',
  purge_eligible_at: new Date(Date.now() + 5 * 86_400_000).toISOString(),
  objects_purged_at: null,
  created_at: '2026-08-10T11:59:00', updated_at: '2026-08-10T12:00:05',
  embedding_started_at: null, completed_at: null, failed_at: null,
};


describe('ImageImportTaskDrawer', () => {
  it('shows all persisted states, safe details and completed asset navigation', () => {
    const onOpenAsset = vi.fn();
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={items}
      total={4}
      page={1}
      pageSize={20}
      unresolvedCount={3}
      processingCount={2}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={onOpenAsset}
    />);

    expect(screen.getByText('等待中')).toBeInTheDocument();
    expect(screen.getByText('生成向量中')).toBeInTheDocument();
    expect(screen.getByText('已完成')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('处理失败（InvalidEmbeddingResult）'))
      .toBeInTheDocument();
    expect(screen.getByText('imports/c/0001/完成图片.png'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看待归款资产' }));
    expect(onOpenAsset).toHaveBeenCalledWith('asset-19');
    expect(screen.queryByRole('button', { name: /重试|取消任务|放弃/ }))
      .not.toBeInTheDocument();
  });

  it('shows server error and empty persisted state without invented progress', () => {
    const { rerender } = render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[]}
      total={0}
      page={1}
      pageSize={20}
      unresolvedCount={0}
      processingCount={0}
      loading={false}
      error="服务端状态读取失败"
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
    />);
    expect(screen.getByText('服务端状态读取失败')).toBeInTheDocument();

    rerender(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[]}
      total={0}
      page={1}
      pageSize={20}
      unresolvedCount={0}
      processingCount={0}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
    />);
    expect(screen.getByText('暂无图片导入任务')).toBeInTheDocument();
  });

  it('shows awaiting-retry schedule, error summary and manual retry action', () => {
    const onRetryItem = vi.fn();
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[awaitingRetryItem, items[3]]}
      total={2}
      page={1}
      pageSize={20}
      unresolvedCount={2}
      processingCount={0}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
      onRetryItem={onRetryItem}
    />);

    expect(screen.getAllByText('等待重试').length).toBeGreaterThan(0);
    expect(screen.getByText(/已尝试 2\/5 次/)).toBeInTheDocument();
    expect(screen.getByText(/原因：服务限流/)).toBeInTheDocument();
    expect(screen.getByText(/失败原因：向量结果不可用/)).toBeInTheDocument();

    const retryButtons = screen.getAllByRole('button', { name: '手工重试' });
    expect(retryButtons).toHaveLength(2);
    fireEvent.click(retryButtons[0]);
    expect(onRetryItem).toHaveBeenCalledWith('retry-20');
  });

  it('hides manual retry control when retry handler is not provided', () => {
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[awaitingRetryItem]}
      total={1}
      page={1}
      pageSize={20}
      unresolvedCount={1}
      processingCount={0}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
    />);

    expect(screen.queryByRole('button', { name: '手工重试' }))
      .not.toBeInTheDocument();
  });

  it('offers cancel selection for cancelable items and a bulk cancel action', () => {
    const onCancelSelectionChange = vi.fn();
    const onBulkCancel = vi.fn();
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={items}
      total={4}
      page={1}
      pageSize={20}
      unresolvedCount={3}
      processingCount={2}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
      selectedCancelIds={['queued-19']}
      onCancelSelectionChange={onCancelSelectionChange}
      onBulkCancel={onBulkCancel}
    />);

    // 可取消项（queued/embedding/failed）出现选择框；completed 没有
    expect(screen.getByRole('checkbox', { name: '选择取消 等待图片.png' }))
      .toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择取消 识别图片.png' }))
      .not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择取消 失败图片.png' }))
      .not.toBeChecked();
    expect(screen.queryByRole('checkbox', { name: '选择取消 完成图片.png' }))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消选中导入' }));
    expect(onBulkCancel).toHaveBeenCalledTimes(1);
  });

  it('shows cancelled terminal state and pending cancel hint', () => {
    const pendingCancel: ImageImportItem = {
      ...cancelledItem,
      item_id: 'pending-21',
      status: 'embedding',
      cancelled_at: null,
    };
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[cancelledItem, pendingCancel]}
      total={2}
      page={1}
      pageSize={20}
      unresolvedCount={0}
      processingCount={1}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
    />);

    expect(screen.getAllByText('已取消').length).toBeGreaterThan(0);
    expect(screen.getByText(/取消时间：/)).toBeInTheDocument();
    expect(screen.getByText(/已请求取消，等待 worker 确认后不会形成正式资产/))
      .toBeInTheDocument();
  });

  it('shows retention window and offers restore/abandon for terminal items', () => {
    const onRestoreItem = vi.fn();
    const onAbandonItem = vi.fn();
    const abandonedItem: ImageImportItem = {
      ...cancelledItem,
      item_id: 'abandoned-22',
      status: 'abandoned',
      purge_eligible_at: new Date(Date.now() - 1000).toISOString(),
    };
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[cancelledItem, abandonedItem]}
      total={2}
      page={1}
      pageSize={20}
      unresolvedCount={0}
      processingCount={0}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
      onRestoreItem={onRestoreItem}
      onAbandonItem={onAbandonItem}
    />);

    // 窗口内的取消项显示剩余天数并可恢复/放弃
    expect(screen.getByText(/暂存对象保留剩余 5 天/)).toBeInTheDocument();
    const restoreButton = screen.getByRole('button', { name: '恢复导入' });
    fireEvent.click(restoreButton);
    expect(onRestoreItem).toHaveBeenCalledWith('cancelled-21');

    const abandonButtons = screen.getAllByRole('button', { name: '提前放弃' });
    fireEvent.click(abandonButtons[0]);
    expect(onAbandonItem).toHaveBeenCalled();

    expect(screen.getByText('已放弃')).toBeInTheDocument();
  });

  it('shows purged marker instead of window after cleanup', () => {
    const purgedItem: ImageImportItem = {
      ...cancelledItem,
      item_id: 'purged-22',
      purge_eligible_at: new Date(Date.now() - 86_400_000).toISOString(),
      objects_purged_at: new Date(Date.now() - 3600_000).toISOString(),
    };
    render(<ImageImportTaskDrawer
      open
      onClose={() => undefined}
      items={[purgedItem]}
      total={1}
      page={1}
      pageSize={20}
      unresolvedCount={0}
      processingCount={0}
      loading={false}
      error={null}
      onPageChange={() => undefined}
      onRefresh={() => undefined}
      onOpenAsset={() => undefined}
      onRestoreItem={() => undefined}
      onAbandonItem={() => undefined}
    />);

    expect(screen.getByText('暂存对象已清理')).toBeInTheDocument();
    expect(screen.queryByText(/暂存对象保留剩余/)).not.toBeInTheDocument();
    // 已清理项不再提供恢复入口
    expect(screen.queryByRole('button', { name: '恢复导入' }))
      .not.toBeInTheDocument();
  });
});

