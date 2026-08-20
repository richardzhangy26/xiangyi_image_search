import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ImportImagesModal } from './ImportImagesModal';
import * as api from '../services/productApi';

vi.mock('../services/productApi', () => {
  class ImageAssetImportRequestError extends Error {
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

  return {
    importImageAssets: vi.fn(),
    ImageAssetImportRequestError,
    getImageUrl: (path: string) => path,
  };
});

// jsdom 不实现 Object URL；导入预览只要求可调用。
globalThis.URL.createObjectURL = vi.fn(() => 'blob:fake-preview');
globalThis.URL.revokeObjectURL = vi.fn();

const makeFile = (name: string, type = 'image/png'): File =>
  new File(['x'], name, { type });

const makeRequestError = (
  status: number | null,
  errorCode: string | null = null,
  message = '图片导入失败'
) => {
  const RequestError = (api as unknown as {
    ImageAssetImportRequestError: new (
      errorMessage: string,
      errorStatus: number | null,
      code: string | null
    ) => Error;
  }).ImageAssetImportRequestError;
  return new RequestError(message, status, errorCode);
};

const renderModal = (props: Partial<Parameters<typeof ImportImagesModal>[0]> = {}) =>
  render(
    <ImportImagesModal
      open
      onClose={props.onClose ?? vi.fn()}
      onFinished={props.onFinished ?? vi.fn()}
      onOpenRecycleBin={props.onOpenRecycleBin ?? vi.fn()}
    />
  );

const selectFiles = (files: File[]) => {
  const inputs = document.querySelectorAll('input[type="file"]');
  fireEvent.change(inputs[0], { target: { files } });
};

describe('ImportImagesModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('disables the import button before any file is selected', () => {
    renderModal();

    expect(screen.getByRole('button', { name: '开始导入（0 张）' })).toBeDisabled();
  });

  it('lists selected files with editable target paths', () => {
    renderModal();

    selectFiles([makeFile('a.png'), makeFile('b.jpg', 'image/jpeg')]);

    expect(screen.getByDisplayValue('a.png')).toBeInTheDocument();
    expect(screen.getByDisplayValue('b.jpg')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始导入（2 张）' })).toBeEnabled();
  });

  it('blocks submission when target paths collide in the batch', () => {
    renderModal();

    selectFiles([makeFile('a.png'), makeFile('b.png')]);
    fireEvent.change(screen.getByDisplayValue('b.png'), {
      target: { value: 'a.png' },
    });

    expect(screen.getByText('存在重复的目标路径，请修改后再导入')).toBeInTheDocument();
    expect(screen.getAllByDisplayValue('a.png')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '开始导入（2 张）' })).toBeDisabled();
  });

  it('submits confirmed names and reports per-item results', async () => {
    const onFinished = vi.fn();
    vi.mocked(api.importImageAssets).mockResolvedValue({
      items: [{
        relative_path: '手动导入/a.png',
        status: 'created',
        asset_id: 'asset-1',
        error: null,
        recovery_action: null,
      }],
      created_count: 1,
      existing_count: 0,
      conflict_count: 0,
      recycle_bin_count: 0,
      skipped_count: 0,
      failed_count: 0,
    });
    renderModal({ onFinished });

    selectFiles([makeFile('a.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(1));
    const [files, paths, prefix] = vi.mocked(api.importImageAssets).mock.calls[0];
    expect(files.map((file) => file.name)).toEqual(['a.png']);
    expect(paths).toEqual(['a.png']);
    expect(prefix).toBe('手动导入');

    expect(await screen.findByText('成功 1')).toBeInTheDocument();
    expect(screen.getByText('手动导入/a.png')).toBeInTheDocument();
    expect(onFinished).toHaveBeenCalled();
  });

  it('shows idempotent, conflict, recycle-bin, and failed outcomes separately', async () => {
    vi.mocked(api.importImageAssets).mockResolvedValue({
      items: [
        {
          relative_path: '手动导入/a.png',
          status: 'existing',
          asset_id: 'asset-a',
          error: null,
          recovery_action: null,
        },
        {
          relative_path: '手动导入/b.png',
          status: 'source_conflict',
          asset_id: 'asset-b',
          error: '来源冲突：同一路径已存在不同内容的图片',
          recovery_action: null,
        },
        {
          relative_path: '手动导入/c.png',
          status: 'in_recycle_bin',
          asset_id: 'asset-c',
          error: null,
          recovery_action: { type: 'open_recycle_bin', asset_id: 'asset-c' },
        },
        {
          relative_path: '手动导入/d.png',
          status: 'failed',
          asset_id: null,
          error: '图片识别服务暂不可用',
          recovery_action: null,
        },
      ],
      created_count: 0,
      existing_count: 1,
      conflict_count: 1,
      recycle_bin_count: 1,
      failed_count: 1,
      skipped_count: 0,
    });
    renderModal();

    selectFiles([
      makeFile('a.png'), makeFile('b.png'),
      makeFile('c.png'), makeFile('d.png'),
    ]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（4 张）' }));

    expect(await screen.findByText('已存在（幂等） 1')).toBeInTheDocument();
    expect(screen.getByText('来源冲突 1')).toBeInTheDocument();
    expect(screen.getByText('在回收站 1')).toBeInTheDocument();
    expect(screen.getByText('失败 1')).toBeInTheDocument();
    expect(screen.queryByText(/内容重复/)).not.toBeInTheDocument();
  });

  it('retries only response items whose status is failed', async () => {
    vi.mocked(api.importImageAssets)
      .mockResolvedValueOnce({
        items: [
          {
            relative_path: '手动导入/a.png', status: 'created',
            asset_id: 'asset-a', error: null, recovery_action: null,
          },
          {
            relative_path: '手动导入/b.png', status: 'failed',
            asset_id: null, error: '图片识别服务暂不可用', recovery_action: null,
          },
        ],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 1, skipped_count: 0,
      })
      .mockResolvedValueOnce({
        items: [{
          relative_path: '手动导入/b.png', status: 'created',
          asset_id: 'asset-b', error: null, recovery_action: null,
        }],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
      });
    renderModal();
    selectFiles([makeFile('a.png'), makeFile('b.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.importImageAssets).mock.calls[1][0]
      .map((file) => file.name)).toEqual(['b.png']);
    expect(vi.mocked(api.importImageAssets).mock.calls[1][1]).toEqual(['b.png']);
  });

  it('stable row identity replaces normalized-path failure after retry', async () => {
    vi.mocked(api.importImageAssets)
      .mockResolvedValueOnce({
        items: [{
          relative_path: '手动导入/folder/a.png', status: 'failed',
          asset_id: null, error: '首次导入失败', recovery_action: null,
        }],
        created_count: 0, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 1, skipped_count: 0,
      })
      .mockResolvedValueOnce({
        items: [{
          relative_path: '手动导入/folder/a.png', status: 'created',
          asset_id: 'asset-a', error: null, recovery_action: null,
        }],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
      });
    renderModal();
    selectFiles([makeFile('a.png')]);
    fireEvent.change(screen.getByDisplayValue('a.png'), {
      target: { value: 'folder//a.png' },
    });
    fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText('手动导入/folder/a.png')).toHaveLength(1);
    expect(screen.queryByText('首次导入失败')).not.toBeInTheDocument();
    expect(screen.queryByText('失败 1')).not.toBeInTheDocument();
    expect(screen.getByText('成功 1')).toBeInTheDocument();
  });

  it('preserves original order when retrying the first row', async () => {
    vi.mocked(api.importImageAssets)
      .mockResolvedValueOnce({
        items: [
          {
            relative_path: '手动导入/a.png', status: 'failed',
            asset_id: null, error: '第一项失败', recovery_action: null,
          },
          {
            relative_path: '手动导入/b.png', status: 'created',
            asset_id: 'asset-b', error: null, recovery_action: null,
          },
        ],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 1, skipped_count: 0,
      })
      .mockResolvedValueOnce({
        items: [{
          relative_path: '手动导入/a.png', status: 'created',
          asset_id: 'asset-a', error: null, recovery_action: null,
        }],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
      });
    renderModal();
    selectFiles([makeFile('a.png'), makeFile('b.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
    const resultRows = Array.from(document.querySelectorAll('tbody tr'));
    expect(resultRows).toHaveLength(2);
    expect(resultRows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('手动导入/a.png'),
      expect.stringContaining('手动导入/b.png'),
    ]);
    expect(screen.queryByText('第一项失败')).not.toBeInTheDocument();
  });

  it('closes and delegates recycle-bin navigation without restoring', async () => {
    const onClose = vi.fn();
    const onOpenRecycleBin = vi.fn();
    vi.mocked(api.importImageAssets).mockResolvedValue({
      items: [{
        relative_path: '手动导入/a.png', status: 'in_recycle_bin',
        asset_id: 'archived-a', error: null,
        recovery_action: { type: 'open_recycle_bin', asset_id: 'archived-a' },
      }],
      created_count: 0, existing_count: 0, conflict_count: 0,
      recycle_bin_count: 1, failed_count: 0, skipped_count: 0,
    });
    renderModal({ onClose, onOpenRecycleBin });
    selectFiles([makeFile('a.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '前往回收站' }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpenRecycleBin).toHaveBeenCalledTimes(1);
  });

  it('retries the entire chunk after a network failure', async () => {
    vi.mocked(api.importImageAssets)
      .mockRejectedValueOnce(makeRequestError(null, null, '网络连接失败'))
      .mockResolvedValueOnce({
        items: [
          {
            relative_path: '手动导入/a.png', status: 'existing',
            asset_id: 'asset-a', error: null, recovery_action: null,
          },
          {
            relative_path: '手动导入/b.png', status: 'created',
            asset_id: 'asset-b', error: null, recovery_action: null,
          },
        ],
        created_count: 1, existing_count: 1, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
      });
    renderModal();
    selectFiles([makeFile('a.png'), makeFile('b.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.importImageAssets).mock.calls[1][0]
      .map((file) => file.name)).toEqual(['a.png', 'b.png']);
    expect(vi.mocked(api.importImageAssets).mock.calls[1][1])
      .toEqual(['a.png', 'b.png']);
  });

  it.each([400, 413])('renders %s as terminal failure without retrying', async (status) => {
    vi.mocked(api.importImageAssets).mockRejectedValueOnce(
      makeRequestError(status, 'IMAGE_IMPORT_REJECTED', `HTTP ${status}`)
    );
    renderModal();
    selectFiles([makeFile('a.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));

    expect(await screen.findByText(`HTTP ${status}`)).toBeInTheDocument();
    expect(screen.getByText('失败 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试失败项' })).not.toBeInTheDocument();
    expect(api.importImageAssets).toHaveBeenCalledTimes(1);
  });

  it('retries the entire chunk after a 503 failure', async () => {
    vi.mocked(api.importImageAssets)
      .mockRejectedValueOnce(makeRequestError(503, 'UPSTREAM_UNAVAILABLE', '服务暂不可用'))
      .mockResolvedValueOnce({
        items: [{
          relative_path: '手动导入/a.png', status: 'created',
          asset_id: 'asset-a', error: null, recovery_action: null,
        }],
        created_count: 1, existing_count: 0, conflict_count: 0,
        recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
      });
    renderModal();
    selectFiles([makeFile('a.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
  });
});
