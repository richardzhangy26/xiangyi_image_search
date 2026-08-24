import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProductUpload } from './ProductUpload';
import * as api from '../services/productApi';

vi.mock('../services/productApi', () => ({
  getProducts: vi.fn(),
  getImageAssets: vi.fn(),
  getArchivedImageAssets: vi.fn(),
  getImageImportItems: vi.fn(),
  createImageImports: vi.fn(),
  retryImageImportItem: vi.fn(),
  cancelImageImportItems: vi.fn(),
  restoreImageImportItem: vi.fn(),
  abandonImageImportItem: vi.fn(),
  renameImageAsset: vi.fn(),
  assignImageAssets: vi.fn(),
  archiveImageAssets: vi.fn(),
  restoreImageAssets: vi.fn(),
  createProduct: vi.fn(),
  updateProduct: vi.fn(),
  deleteProductImage: vi.fn(),
  deleteProduct: vi.fn(),
  batchDeleteProducts: vi.fn(),
  importProductsFromCSV: vi.fn(),
  downloadCSVTemplate: vi.fn(),
  importImageAssets: vi.fn(),
  buildVectorIndex: vi.fn(() => () => undefined),
  getImageUrl: (path: string) => path,
  getPurgeReadiness: vi.fn(),
}));

interface RecycleBinApi {
  getArchivedImageAssets: (params: {
    page: number;
    perPage: number;
    search?: string;
  }) => Promise<typeof archivedResponse>;
  restoreImageAssets: (assetIds: string[]) => Promise<{
    batch_id: string;
    status: 'succeeded' | 'rejected';
    restored_count: number;
    already_active_count: number;
    items: Array<{
      asset_id: string;
      status: 'restored' | 'already_active' | 'unchanged' | 'rejected';
      version: number | null;
    }>;
  }>;
}

const recycleBinApi = api as typeof api & RecycleBinApi;

const assetResponse = {
  assets: [{
    asset_id: 'asset-1',
    model_number: null,
    display_name: '待归款业务名.png',
    source_relative_path: '手机挂绳/A47/修改后/2.png',
    version: 1,
    status: 'active' as const,
    archived_at: null,
    preview_url: '/api/image-assets/asset-1/preview',
    source_size: 58_896_865,
    source_mime_type: 'image/png',
    source_width: 6000,
    source_height: 4000,
    created_at: '2026-08-02T11:30:00',
  }],
  total: 2419,
  page: 1,
  per_page: 24,
};

const archivedAsset = {
  ...assetResponse.assets[0],
  asset_id: 'archived-1',
  display_name: '蓝色挂绳.png',
  source_relative_path: '回收站/挂绳/A47/蓝色.png',
  version: 3,
  status: 'archived' as const,
  archived_at: '2026-08-09T12:00:00',
  preview_url: '/api/image-assets/archived-1/preview',
};

const secondArchivedAsset = {
  ...archivedAsset,
  asset_id: 'archived-2',
  display_name: '红色挂绳.png',
  source_relative_path: '回收站/挂绳/A47/红色.png',
  preview_url: '/api/image-assets/archived-2/preview',
};

const archivedResponse = {
  assets: [archivedAsset],
  total: 1,
  archived_total: 37,
  page: 1,
  per_page: 24,
};

describe('ProductUpload unified management view', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [], total: 0, page: 0, per_page: 20,
    });
    vi.mocked(api.getImageAssets).mockResolvedValue(assetResponse);
    vi.mocked(recycleBinApi.getArchivedImageAssets)
      .mockResolvedValue(archivedResponse);
    vi.mocked(recycleBinApi.restoreImageAssets).mockResolvedValue({
      batch_id: 'restore-batch-default',
      status: 'succeeded',
      restored_count: 1,
      already_active_count: 0,
      items: [{
        asset_id: 'archived-1', status: 'restored', version: 4,
      }],
    });
    vi.mocked(api.getImageImportItems).mockResolvedValue({
      items: [{
        item_id: 'failed-task-19',
        display_name: '失败任务.png',
        source_relative_path: 'imports/hash/0001/失败任务.png',
        source_revision: 1,
        status: 'failed',
        asset_id: null,
        failure_message: '处理失败（InvalidEmbeddingResult）',
        attempt_count: 5,
        max_auto_attempts: 5,
        last_error_class: 'embedding_incompatible',
        last_attempt_at: '2026-08-09T12:00:30',
        next_retry_at: null,
        cancel_requested_at: null,
        cancelled_at: null,
        purge_eligible_at: null,
        objects_purged_at: null,
        created_at: '2026-08-09T12:00:00',
        updated_at: '2026-08-09T12:01:00',
        embedding_started_at: '2026-08-09T12:00:30',
        completed_at: null,
        failed_at: '2026-08-09T12:01:00',
      }],
      total: 1,
      page: 1,
      per_page: 20,
      unresolved_count: 1,
      processing_count: 0,
    });
    vi.mocked(api.createImageImports).mockResolvedValue({
      queued_count: 1,
      items: [{
        item_id: 'queued-task-19',
        asset_id: null,
        source_relative_path: 'imports/hash/0001/new.png',
        status: 'queued',
        recovery_action: null,
      }],
    });
  });

  const submitNewProduct = async () => {
    // 工具栏与资产工作台各有一个「添加产品」：工具栏在前，打开完整产品表单
    const addButtons = await screen.findAllByRole('button', { name: /添加产品/ });
    fireEvent.click(addButtons[0]);
    fireEvent.change(screen.getByPlaceholderText('如: CS-001'), {
      target: { value: 'MODEL-18' },
    });
    fireEvent.change(screen.getByPlaceholderText('如: photographer_001'), {
      target: { value: 'photo' },
    });
    fireEvent.change(screen.getByPlaceholderText(
      'https://detail.1688.com/offer/...'
    ), { target: { value: 'https://example.test/item' } });
    fireEvent.change(screen.getByPlaceholderText('如: 相机肩带'), {
      target: { value: '挂绳' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));
  };

  it('restores persisted import status and opens the unresolved task drawer', async () => {
    render(<ProductUpload />);

    await waitFor(() => expect(api.getImageImportItems).toHaveBeenCalledWith({
      page: 1, perPage: 20,
    }));
    expect(await screen.findByText('导入任务')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '导入任务' }));

    expect(await screen.findByText('失败任务.png')).toBeInTheDocument();
    expect(screen.getByText('处理失败（InvalidEmbeddingResult）'))
      .toBeInTheDocument();
    // Issue #20 取代原「无重试控件」断言：失败项提供幂等手工重试入口
    vi.mocked(api.retryImageImportItem).mockResolvedValue({
      item_id: 'failed-task-19',
      display_name: '失败任务.png',
      source_relative_path: 'imports/hash/0001/失败任务.png',
      source_revision: 1,
      status: 'awaiting_retry',
      asset_id: null,
      failure_message: '处理失败（InvalidEmbeddingResult）',
      attempt_count: 5,
      max_auto_attempts: 5,
      last_error_class: 'embedding_incompatible',
      last_attempt_at: '2026-08-09T12:00:30',
      next_retry_at: '2026-08-09T12:02:00',
      cancel_requested_at: null,
      cancelled_at: null,
      purge_eligible_at: null,
      objects_purged_at: null,
      created_at: '2026-08-09T12:00:00',
      updated_at: '2026-08-09T12:01:00',
      embedding_started_at: '2026-08-09T12:00:30',
      completed_at: null,
      failed_at: '2026-08-09T12:01:00',
    });
    fireEvent.click(screen.getByRole('button', { name: '手工重试' }));
    await waitFor(() => expect(api.retryImageImportItem)
      .toHaveBeenCalledWith('failed-task-19'));
    // Issue #22 终态：失败项同时提供手工重试与提前放弃入口
    expect(screen.getByRole('button', { name: '提前放弃' }))
      .toBeInTheDocument();
  });

  it('cancels a selected import item through the bulk cancel action', async () => {
    vi.mocked(api.cancelImageImportItems).mockResolvedValue({
      items: [{ item_id: 'failed-task-19', result: 'cancelled' }],
      cancelled_count: 1,
      batch_id: 'batch-21',
    });
    render(<ProductUpload />);

    await waitFor(() => expect(api.getImageImportItems).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: '导入任务' }));

    const checkbox = await screen.findByRole('checkbox', {
      name: '选择取消 失败任务.png',
    });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: '取消选中导入' }));

    await waitFor(() => expect(api.cancelImageImportItems)
      .toHaveBeenCalledWith(['failed-task-19']));
  });

  it('restores a cancelled import item within the retention window', async () => {
    vi.mocked(api.getImageImportItems).mockResolvedValue({
      items: [{
        item_id: 'cancelled-task-22',
        display_name: '已取消任务.png',
        source_relative_path: 'imports/hash/0001/已取消任务.png',
        source_revision: 1,
        status: 'cancelled',
        asset_id: null,
        failure_message: null,
        attempt_count: 0,
        max_auto_attempts: 5,
        last_error_class: null,
        last_attempt_at: null,
        next_retry_at: null,
        cancel_requested_at: '2026-08-09T12:00:00',
        cancelled_at: '2026-08-09T12:00:00',
        purge_eligible_at: new Date(Date.now() + 5 * 86_400_000)
          .toISOString(),
        objects_purged_at: null,
        created_at: '2026-08-09T12:00:00',
        updated_at: '2026-08-09T12:00:00',
        embedding_started_at: null,
        completed_at: null,
        failed_at: null,
      }],
      total: 1,
      page: 1,
      per_page: 20,
      unresolved_count: 0,
      processing_count: 0,
    });
    vi.mocked(api.restoreImageImportItem).mockResolvedValue({
      item_id: 'cancelled-task-22',
      display_name: '已取消任务.png',
      source_relative_path: 'imports/hash/0001/已取消任务.png',
      source_revision: 1,
      status: 'queued',
      asset_id: null,
      failure_message: null,
      attempt_count: 0,
      max_auto_attempts: 5,
      last_error_class: null,
      last_attempt_at: null,
      next_retry_at: null,
      cancel_requested_at: null,
      cancelled_at: null,
      purge_eligible_at: null,
      objects_purged_at: null,
      created_at: '2026-08-09T12:00:00',
      updated_at: '2026-08-09T12:01:00',
      embedding_started_at: null,
      completed_at: null,
      failed_at: null,
    });
    render(<ProductUpload />);

    await waitFor(() => expect(api.getImageImportItems).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: '导入任务' }));

    fireEvent.click(await screen.findByRole('button', { name: '恢复导入' }));
    await waitFor(() => expect(api.restoreImageImportItem)
      .toHaveBeenCalledWith('cancelled-task-22'));
  });

  it('abandons a failed import only after the irreversible confirmation', async () => {
    vi.mocked(api.abandonImageImportItem).mockResolvedValue({
      item_id: 'failed-task-19',
      display_name: '失败任务.png',
      source_relative_path: 'imports/hash/0001/失败任务.png',
      source_revision: 1,
      status: 'abandoned',
      asset_id: null,
      failure_message: '处理失败（InvalidEmbeddingResult）',
      attempt_count: 5,
      max_auto_attempts: 5,
      last_error_class: 'embedding_incompatible',
      last_attempt_at: '2026-08-09T12:00:30',
      next_retry_at: null,
      cancel_requested_at: null,
      cancelled_at: null,
      purge_eligible_at: new Date().toISOString(),
      objects_purged_at: null,
      created_at: '2026-08-09T12:00:00',
      updated_at: '2026-08-09T12:01:00',
      embedding_started_at: '2026-08-09T12:00:30',
      completed_at: null,
      failed_at: '2026-08-09T12:01:00',
    });
    render(<ProductUpload />);

    await waitFor(() => expect(api.getImageImportItems).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: '导入任务' }));

    fireEvent.click(await screen.findByRole('button', { name: '提前放弃' }));
    // 确认弹窗出现前不得调用放弃接口
    expect(api.abandonImageImportItem).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole('button', { name: '确认放弃' }));
    await waitFor(() => expect(api.abandonImageImportItem)
      .toHaveBeenCalledWith('failed-task-19'));
  });

  it('queues standalone images then reloads server tasks and unassigned assets', async () => {
    render(<ProductUpload />);
    fireEvent.click(await screen.findByRole('button', { name: /导入图片/ }));
    const input = document.querySelector(
      'input[type="file"][accept="image/*"]'
    ) as HTMLInputElement;
    const file = new File(['image'], 'new.png', { type: 'image/png' });
    fireEvent.change(input, { target: { files: [file] } });
    const startButton = screen.getByRole('button', { name: '开始导入' });
    await waitFor(() => expect(startButton).toBeEnabled());
    fireEvent.click(startButton);

    await waitFor(() => expect(api.createImageImports).toHaveBeenCalledWith([file]));
    await waitFor(() => expect(api.getImageImportItems).toHaveBeenCalledTimes(2));
    expect(api.getImageAssets).toHaveBeenCalledTimes(2);
  });

  it('shows created and idempotent source outcomes separately', async () => {
    vi.mocked(api.createProduct).mockResolvedValue({
      message: '产品创建成功',
      model_number: 'MODEL-18',
      uploaded_images: 1,
      reused_images: 1,
      recycle_bin_images: 0,
      skipped_duplicates: ['asset-existing'],
      image_results: [
        {
          asset_id: 'asset-new', source_relative_path: 'catalog/new.png',
          status: 'created',
        },
        {
          asset_id: 'asset-existing',
          source_relative_path: 'catalog/existing.png', status: 'existing',
        },
      ],
    });
    render(<ProductUpload />);

    await submitNewProduct();

    expect(await screen.findByText('成功导入 1 张新图片')).toBeInTheDocument();
    expect(screen.getByText('1 张图片已按来源身份幂等复用'))
      .toBeInTheDocument();
  });

  it('keeps a recycle-bin hit visible and navigates without restoring', async () => {
    vi.mocked(api.createProduct).mockResolvedValue({
      message: '产品创建成功',
      model_number: 'MODEL-18',
      uploaded_images: 0,
      reused_images: 0,
      recycle_bin_images: 1,
      skipped_duplicates: [],
      image_results: [{
        asset_id: 'archived-1',
        source_relative_path: '回收站/挂绳/A47/蓝色.png',
        status: 'in_recycle_bin',
        recovery_action: {
          type: 'open_recycle_bin', asset_id: 'archived-1',
        },
      }],
    });
    render(<ProductUpload />);

    await submitNewProduct();

    expect(await screen.findByText('1 张图片已在回收站，未自动恢复'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '前往回收站' }));

    expect(await screen.findByRole('region', { name: '回收站' }))
      .toBeInTheDocument();
    expect(recycleBinApi.restoreImageAssets).not.toHaveBeenCalled();
    expect(recycleBinApi.getArchivedImageAssets).toHaveBeenLastCalledWith({
      page: 1, perPage: 24, search: '',
    });
  });

  it('wires local-import recycle hits to the archived view without restore', async () => {
    vi.mocked(api.importImageAssets).mockResolvedValue({
      items: [{
        relative_path: '手动导入/a.png', status: 'in_recycle_bin',
        asset_id: 'archived-local-a', error: null,
        recovery_action: {
          type: 'open_recycle_bin', asset_id: 'archived-local-a',
        },
      }],
      created_count: 0, existing_count: 0, conflict_count: 0,
      recycle_bin_count: 1, failed_count: 0, skipped_count: 0,
    });
    const { container } = render(<ProductUpload />);
    fireEvent.click(await within(container).findByText('本地导入'));
    const dialog = await screen.findByRole('dialog', {
      name: '导入图片到待归款',
    });
    const fileInput = dialog.querySelector('input[type="file"]');
    if (!(fileInput instanceof HTMLInputElement)) {
      throw new Error('local import file input not found');
    }
    fireEvent.change(fileInput, {
      target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] },
    });
    fireEvent.click(within(dialog).getByRole('button', {
      name: '开始导入（1 张）',
    }));
    fireEvent.click(await within(dialog).findByRole('button', {
      name: '前往回收站',
    }));

    expect(await screen.findByRole('region', { name: '回收站' }))
      .toBeInTheDocument();
    expect(recycleBinApi.restoreImageAssets).not.toHaveBeenCalled();
    expect(recycleBinApi.getArchivedImageAssets).toHaveBeenLastCalledWith({
      page: 1,
      perPage: 24,
      search: '',
    });
  });

  it('shows a dedicated source-conflict message and keeps the form open', async () => {
    const conflict = Object.assign(
      new Error('来源冲突：同一来源身份已存在不同内容，未覆盖现有资产'),
      { errorCode: 'IMAGE_ASSET_SOURCE_CONFLICT' }
    );
    vi.mocked(api.createProduct).mockRejectedValue(conflict);
    render(<ProductUpload />);

    await submitNewProduct();

    expect(await screen.findByText('来源身份冲突，现有图片未被覆盖'))
      .toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '添加产品' }))
      .toBeInTheDocument();
  });

  it('defaults to real unassigned assets when there are no products', async () => {
    render(<ProductUpload />);

    expect(await screen.findByText(
      '待归款业务名.png'
    )).toBeInTheDocument();
    expect(screen.getByText('2,419 张图片资产')).toBeInTheDocument();
    expect(screen.getByText('0 个产品')).toBeInTheDocument();
    const workbench = within(screen.getByRole('region', { name: '图片资产' }));
    expect(workbench.getByRole('button', { name: '关联型号' })).toBeDisabled();
    expect(workbench.getByRole('button', { name: '添加产品' })).toBeEnabled();
    expect(api.getImageAssets).toHaveBeenCalledWith({
      assignment: 'unassigned', page: 1, perPage: 24, search: '',
    });
  });

  it('loads the archived total, opens the recycle bin and searches both fields', async () => {
    render(<ProductUpload />);

    const recycleBinTab = await screen.findByText('回收站 (37)');
    expect(recycleBinApi.getArchivedImageAssets).toHaveBeenCalledWith({
      page: 1, perPage: 24, search: '',
    });

    fireEvent.click(recycleBinTab);
    expect(await screen.findByText('蓝色挂绳.png')).toBeInTheDocument();
    const search = screen.getByPlaceholderText('搜索显示名称或来源路径');
    fireEvent.change(search, { target: { value: '蓝色 A47' } });
    fireEvent.keyDown(search, { key: 'Enter', code: 'Enter' });

    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenLastCalledWith({
        page: 1, perPage: 24, search: '蓝色 A47',
      }));
  });

  it('ignores an older archived response that finishes after a new search', async () => {
    let resolveInitial: (value: typeof archivedResponse) => void = () => undefined;
    const initialRequest = new Promise<typeof archivedResponse>((resolve) => {
      resolveInitial = resolve;
    });
    vi.mocked(recycleBinApi.getArchivedImageAssets)
      .mockImplementationOnce(() => initialRequest)
      .mockResolvedValueOnce({
        ...archivedResponse,
        assets: [secondArchivedAsset],
        archived_total: 38,
      });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('回收站 (0)'));
    const search = screen.getByPlaceholderText('搜索显示名称或来源路径');
    fireEvent.change(search, { target: { value: '红色' } });
    fireEvent.keyDown(search, { key: 'Enter', code: 'Enter' });

    expect(await screen.findByText('红色挂绳.png')).toBeInTheDocument();
    expect(await screen.findByText('回收站 (38)')).toBeInTheDocument();

    await act(async () => {
      resolveInitial({ ...archivedResponse, archived_total: 99 });
      await initialRequest;
    });

    expect(screen.getByText('回收站 (38)')).toBeInTheDocument();
    expect(screen.getByText('红色挂绳.png')).toBeInTheDocument();
    expect(screen.queryByText('蓝色挂绳.png')).not.toBeInTheDocument();
  });

  it('keeps restored assets when an older active request finishes later', async () => {
    let resolveInitial: (value: typeof assetResponse) => void = () => undefined;
    const initialRequest = new Promise<typeof assetResponse>((resolve) => {
      resolveInitial = resolve;
    });
    const restoredAsset = {
      ...archivedAsset,
      display_name: '已恢复蓝色挂绳.png',
      status: 'active' as const,
      archived_at: null,
    };
    vi.mocked(api.getImageAssets)
      .mockImplementationOnce(() => initialRequest)
      .mockResolvedValueOnce({
        ...assetResponse,
        assets: [restoredAsset],
        total: 1,
      });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('回收站 (37)'));
    fireEvent.click(await screen.findByRole('checkbox', {
      name: '选择 蓝色挂绳.png',
    }));
    fireEvent.click(screen.getByRole('button', { name: '恢复选中图片' }));

    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('radio', { name: /图片资产/ }))
      .not.toBeDisabled());

    await act(async () => {
      resolveInitial(assetResponse);
      await initialRequest;
    });

    fireEvent.click(screen.getByText('图片资产 (1)'));
    expect(await screen.findByText('已恢复蓝色挂绳.png')).toBeInTheDocument();
    expect(screen.queryByText('待归款业务名.png')).not.toBeInTheDocument();
  });

  it('refreshes archived and active assets, clears selection and syncs count after restore', async () => {
    vi.mocked(recycleBinApi.getArchivedImageAssets)
      .mockResolvedValueOnce({
        ...archivedResponse,
        assets: [archivedAsset, secondArchivedAsset],
        total: 2,
      })
      .mockResolvedValueOnce({
        ...archivedResponse,
        assets: [secondArchivedAsset],
        total: 1,
        archived_total: 36,
      });
    vi.mocked(recycleBinApi.restoreImageAssets).mockResolvedValue({
      batch_id: 'restore-batch-1',
      status: 'succeeded',
      restored_count: 1,
      already_active_count: 0,
      items: [{
        asset_id: 'archived-1', status: 'restored', version: 4,
      }],
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('回收站 (37)'));
    fireEvent.click(await screen.findByRole('checkbox', {
      name: '选择 蓝色挂绳.png',
    }));
    fireEvent.click(screen.getByRole('button', { name: '恢复选中图片' }));

    await waitFor(() => expect(recycleBinApi.restoreImageAssets)
      .toHaveBeenCalledWith(['archived-1']));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('回收站 (36)')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '恢复选中图片' }))
      .not.toBeInTheDocument();
  });

  it('returns to the first archived page after a successful restore', async () => {
    vi.mocked(recycleBinApi.getArchivedImageAssets).mockResolvedValue({
      ...archivedResponse,
      total: 49,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('回收站 (37)'));
    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenLastCalledWith({ page: 2, perPage: 24, search: '' }));
    fireEvent.click(await screen.findByRole('checkbox', {
      name: '选择 蓝色挂绳.png',
    }));
    fireEvent.click(screen.getByRole('button', { name: '恢复选中图片' }));

    await waitFor(() => expect(recycleBinApi.restoreImageAssets)
      .toHaveBeenCalledWith(['archived-1']));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenLastCalledWith({ page: 1, perPage: 24, search: '' }));
  });

  it('locks top-level controls and keeps selection on restore failure', async () => {
    let rejectRestore: (reason?: unknown) => void = () => undefined;
    const restoreRequest = new Promise<never>((_, reject) => {
      rejectRestore = reject;
    });
    vi.mocked(recycleBinApi.restoreImageAssets)
      .mockImplementationOnce(() => restoreRequest);
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('回收站 (37)'));
    const checkbox = await screen.findByRole('checkbox', {
      name: '选择 蓝色挂绳.png',
    });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: '恢复选中图片' }));

    await waitFor(() => expect(recycleBinApi.restoreImageAssets)
      .toHaveBeenCalledWith(['archived-1']));
    expect(screen.getByRole('radio', { name: /图片资产/ })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /产品资料/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'reload' })).toBeDisabled();

    await act(async () => {
      rejectRestore(new Error('蓝色挂绳.png 已关联型号，无法恢复'));
      await restoreRequest.catch(() => undefined);
    });

    expect(await screen.findByText('蓝色挂绳.png 已关联型号，无法恢复'))
      .toBeInTheDocument();
    expect(checkbox).toBeChecked();
    expect(screen.getByRole('button', { name: '恢复选中图片' }))
      .toBeInTheDocument();
    expect(recycleBinApi.getArchivedImageAssets).toHaveBeenCalledTimes(1);
    expect(api.getImageAssets).toHaveBeenCalledTimes(1);
  });

  it('archives selected assets only after an explicit searchable-impact confirmation', async () => {
    vi.mocked(recycleBinApi.getArchivedImageAssets)
      .mockResolvedValueOnce(archivedResponse)
      .mockResolvedValueOnce({ ...archivedResponse, archived_total: 38 });
    vi.mocked(api.archiveImageAssets).mockResolvedValue({
      batch_id: 'batch-1', status: 'succeeded', archived_count: 1,
      already_archived_count: 0, items: [],
    });
    render(<ProductUpload />);

    expect(screen.getByRole('button', { name: '移入回收站' })).toBeDisabled();
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));

    expect(screen.getByText(/普通搜索/)).toBeInTheDocument();
    expect(screen.getByText(/向量搜索/)).toBeInTheDocument();
    expect(screen.getByText(/可从回收站恢复/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认移入回收站' }));

    await waitFor(() => expect(api.archiveImageAssets)
      .toHaveBeenCalledWith(['asset-1']));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
    expect(await screen.findByText('回收站 (38)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '移入回收站' })).toBeDisabled();
  });

  it('does not archive assets when the confirmation is cancelled', async () => {
    render(<ProductUpload />);

    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));
    fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));

    expect(api.archiveImageAssets).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '移入回收站' }))
      .toBeInTheDocument();
  });

  it('keeps the confirmation and selection when archiving fails', async () => {
    vi.mocked(api.archiveImageAssets).mockRejectedValue(
      new Error('图片已经归款，无法移入回收站')
    );
    render(<ProductUpload />);

    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));
    fireEvent.click(screen.getByRole('button', { name: '确认移入回收站' }));

    await waitFor(() => expect(api.archiveImageAssets)
      .toHaveBeenCalledWith(['asset-1']));
    expect(await screen.findByText('图片已经归款，无法移入回收站'))
      .toBeInTheDocument();
    expect(screen.getByText(/确认将选中的 1 张图片/)).toBeInTheDocument();
    expect(api.getImageAssets).toHaveBeenCalledTimes(1);
  });

  it('refreshes both lists after a successful assignment', async () => {
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [{
        model_number: 'CS-001',
        photographer_file: 'p',
        alibaba_product_url: 'https://example.com',
        category: '挂绳',
      }],
      total: 1,
      page: 0,
      per_page: 20,
    });
    vi.mocked(api.assignImageAssets).mockResolvedValue({
      model_number: 'CS-001', assigned_count: 1, reused_count: 0,
      product_created: false,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '关联型号' }));
    fireEvent.mouseDown(screen.getByRole('combobox'));
    const modelOptions = await screen.findAllByText('CS-001');
    fireEvent.click(modelOptions[modelOptions.length - 1]);
    fireEvent.click(screen.getByRole('button', { name: '确定关联' }));

    await waitFor(() => expect(api.assignImageAssets).toHaveBeenCalledWith(
      ['asset-1'], 'CS-001'
    ));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    expect(recycleBinApi.getArchivedImageAssets).toHaveBeenCalledTimes(2);
    expect(api.getProducts).toHaveBeenCalledTimes(2);
  });

  it('creates a product and assigns when assets are selected', async () => {
    vi.mocked(api.assignImageAssets).mockResolvedValue({
      model_number: 'NEW-001', assigned_count: 1, reused_count: 0,
      product_created: true,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(within(
      screen.getByRole('region', { name: '图片资产' })
    ).getByRole('button', { name: '添加产品' }));
    fireEvent.change(
      screen.getByPlaceholderText('输入新产品型号'),
      { target: { value: 'NEW-001' } }
    );
    fireEvent.click(screen.getByRole('button', { name: '创建并关联' }));

    await waitFor(() => expect(api.assignImageAssets).toHaveBeenCalledWith(
      ['asset-1'], 'NEW-001', { createIfMissing: true }
    ));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    expect(api.getProducts).toHaveBeenCalledTimes(2);
  });

  it('creates a bare product when no assets are selected', async () => {
    vi.mocked(api.createProduct).mockResolvedValue({
      message: '产品创建成功', model_number: 'NEW-002',
      uploaded_images: 0, reused_images: 0, recycle_bin_images: 0,
      skipped_duplicates: [], image_results: [],
    });
    render(<ProductUpload />);

    await screen.findByText('待归款业务名.png');
    fireEvent.click(within(
      screen.getByRole('region', { name: '图片资产' })
    ).getByRole('button', { name: '添加产品' }));
    fireEvent.change(
      screen.getByPlaceholderText('输入新产品型号'),
      { target: { value: 'NEW-002' } }
    );
    fireEvent.click(screen.getByRole('button', { name: '创 建' }));

    await waitFor(() => expect(api.createProduct).toHaveBeenCalledWith(
      { model_number: 'NEW-002' }, []
    ));
    expect(api.assignImageAssets).not.toHaveBeenCalled();
    await waitFor(() => expect(api.getProducts).toHaveBeenCalledTimes(2));
  });

  it('refreshes the recycle-bin count when product editing archives an image', async () => {
    const productWithImage = {
      model_number: 'CS-001',
      photographer_file: 'p',
      alibaba_product_url: 'https://example.com',
      category: '挂绳',
      images: [{
        id: 'assigned-asset-1',
        asset_id: 'assigned-asset-1',
        model_number: 'CS-001',
        image_path: '/api/image-assets/assigned-asset-1/preview',
        preview_url: '/api/image-assets/assigned-asset-1/preview',
        display_name: '待移除挂绳.png',
        source_relative_path: '产品/CS-001/待移除挂绳.png',
        version: 1,
        content_hash: 'a'.repeat(64),
        original_path: null,
        image_order: 0,
        is_primary: true,
      }],
    };
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [productWithImage], total: 1, page: 0, per_page: 20,
    });
    vi.mocked(api.updateProduct).mockResolvedValue({
      message: '产品更新成功', uploaded_images: 0, reused_images: 0,
      recycle_bin_images: 0,
      skipped_duplicates: [], image_results: [],
    });
    vi.mocked(api.deleteProductImage).mockResolvedValue({
      message: '图片已移入回收站',
    });
    vi.mocked(recycleBinApi.getArchivedImageAssets)
      .mockResolvedValueOnce(archivedResponse)
      .mockResolvedValueOnce({ ...archivedResponse, archived_total: 38 });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('产品资料 (1)'));
    fireEvent.click(await screen.findByRole('button', { name: /编辑/ }));
    fireEvent.click(await screen.findByTitle('Remove file'));
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => expect(api.deleteProductImage).toHaveBeenCalledWith(
      'CS-001', 'assigned-asset-1'
    ));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
    expect(await screen.findByText('回收站 (38)')).toBeInTheDocument();
  });

  it('refreshes both lists when product update succeeds but image archive fails', async () => {
    const productWithImages = {
      model_number: 'CS-001',
      photographer_file: 'p',
      alibaba_product_url: 'https://example.com',
      category: '挂绳',
      images: ['assigned-asset-1'].map((assetId) => ({
        id: assetId,
        asset_id: assetId,
        model_number: 'CS-001',
        image_path: `/api/image-assets/${assetId}/preview`,
        preview_url: `/api/image-assets/${assetId}/preview`,
        display_name: `${assetId}.png`,
        source_relative_path: `产品/CS-001/${assetId}.png`,
        version: 1,
        content_hash: assetId.padEnd(64, 'a'),
        original_path: null,
        image_order: 0,
        is_primary: assetId === 'assigned-asset-1',
      })),
    };
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [productWithImages], total: 1, page: 0, per_page: 20,
    });
    vi.mocked(api.updateProduct).mockResolvedValue({
      message: '产品更新成功', uploaded_images: 0, reused_images: 0,
      recycle_bin_images: 0,
      skipped_duplicates: [], image_results: [],
    });
    vi.mocked(api.deleteProductImage).mockRejectedValue(
      new Error('图片归档失败')
    );
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('产品资料 (1)'));
    fireEvent.click(await screen.findByRole('button', { name: /编辑/ }));
    fireEvent.click(await screen.findByTitle('Remove file'));
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => expect(api.deleteProductImage).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/1 张图片归档失败/)).toBeInTheDocument();
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
  });

  it('refreshes active and archived assignment state after deleting a product', async () => {
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [{
        model_number: 'CS-001',
        photographer_file: 'p',
        alibaba_product_url: 'https://example.com',
        category: '挂绳',
      }],
      total: 1,
      page: 0,
      per_page: 20,
    });
    vi.mocked(api.deleteProduct).mockResolvedValue({ message: '产品已删除' });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('产品资料 (1)'));
    fireEvent.click(await screen.findByRole('button', { name: /删除/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));

    await waitFor(() => expect(api.deleteProduct).toHaveBeenCalledWith(
      'CS-001'
    ));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
  });

  it('refreshes active and archived assignment state after batch deletion', async () => {
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [{
        model_number: 'CS-001',
        photographer_file: 'p',
        alibaba_product_url: 'https://example.com',
        category: '挂绳',
      }],
      total: 1,
      page: 0,
      per_page: 20,
    });
    vi.mocked(api.batchDeleteProducts).mockResolvedValue({
      message: '产品已删除', deleted_count: 1,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('产品资料 (1)'));
    const checkboxes = await screen.findAllByRole('checkbox');
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    fireEvent.click(screen.getByRole('button', { name: /批量删除/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));

    await waitFor(() => expect(api.batchDeleteProducts).toHaveBeenCalledWith([
      'CS-001',
    ]));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(recycleBinApi.getArchivedImageAssets)
      .toHaveBeenCalledTimes(2));
  });

  it('opens assigned assets and renames one from the top-level workbench', async () => {
    const assignedAsset = {
      ...assetResponse.assets[0],
      model_number: 'CS-001',
      display_name: '已归款旧名.png',
    };
    vi.mocked(api.getImageAssets).mockImplementation(async (params) => (
      params.assignment === 'assigned'
        ? { ...assetResponse, assets: [assignedAsset], total: 1 }
        : assetResponse
    ));
    vi.mocked(api.renameImageAsset).mockResolvedValue({
      ...assignedAsset,
      display_name: '已归款新名.png',
      version: 2,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByText('已归款'));
    expect(await screen.findByText('已归款旧名.png')).toBeInTheDocument();
    expect(api.getImageAssets).toHaveBeenLastCalledWith({
      assignment: 'assigned', page: 1, perPage: 24, search: '',
    });

    fireEvent.click(screen.getByRole('button', {
      name: '编辑显示名称 已归款旧名.png',
    }));
    const input = screen.getByRole('textbox', { name: '显示名称主体' });
    fireEvent.change(input, { target: { value: '已归款新名' } });
    fireEvent.click(screen.getByRole('button', { name: '保存显示名称' }));

    await waitFor(() => expect(api.renameImageAsset).toHaveBeenCalledWith(
      'asset-1', '已归款新名', 1
    ));
    expect(await screen.findByText('已归款新名.png')).toBeInTheDocument();
  });

  it('refreshes a filtered page after rename changes a search match', async () => {
    vi.mocked(api.getImageAssets)
      .mockResolvedValueOnce(assetResponse)
      .mockResolvedValueOnce(assetResponse)
      .mockResolvedValueOnce({ ...assetResponse, assets: [], total: 0 });
    vi.mocked(api.renameImageAsset).mockResolvedValue({
      ...assetResponse.assets[0],
      display_name: '不再匹配.png',
      version: 2,
    });
    render(<ProductUpload />);

    const search = await screen.findByPlaceholderText(
      '搜索显示名称或来源路径'
    );
    fireEvent.change(search, { target: { value: '待归款业务名' } });
    fireEvent.keyDown(search, { key: 'Enter', code: 'Enter' });
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', {
      name: '编辑显示名称 待归款业务名.png',
    }));
    fireEvent.change(
      screen.getByRole('textbox', { name: '显示名称主体' }),
      { target: { value: '不再匹配' } }
    );
    fireEvent.click(screen.getByRole('button', { name: '保存显示名称' }));

    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(3));
    expect(api.getImageAssets).toHaveBeenLastCalledWith({
      assignment: 'unassigned',
      page: 1,
      perPage: 24,
      search: '待归款业务名',
    });
    expect(screen.queryByText('不再匹配.png')).not.toBeInTheDocument();
  });
});
