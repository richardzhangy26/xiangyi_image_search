import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ImportImagesModal } from './ImportImagesModal';
import * as api from '../services/productApi';

vi.mock('../services/productApi', () => ({
  importImageAssets: vi.fn(),
  getImageUrl: (path: string) => path,
}));

// jsdom 不实现 Object URL；导入预览只要求可调用。
globalThis.URL.createObjectURL = vi.fn(() => 'blob:fake-preview');
globalThis.URL.revokeObjectURL = vi.fn();

const makeFile = (name: string, type = 'image/png'): File =>
  new File(['x'], name, { type });

const renderModal = (props: Partial<Parameters<typeof ImportImagesModal>[0]> = {}) =>
  render(
    <ImportImagesModal
      open
      onClose={props.onClose ?? vi.fn()}
      onFinished={props.onFinished ?? vi.fn()}
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
      }],
      created_count: 1,
      existing_count: 0,
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

  it('shows skipped duplicates and failures in the summary', async () => {
    vi.mocked(api.importImageAssets).mockResolvedValue({
      items: [
        {
          relative_path: '手动导入/a.png',
          status: 'skipped_duplicate_content',
          asset_id: null,
          error: '内容重复：系统中已存在相同内容的图片',
        },
        {
          relative_path: '手动导入/b.png',
          status: 'source_conflict',
          asset_id: null,
          error: '名字重复：同一路径已存在不同内容的图片',
        },
      ],
      created_count: 0,
      existing_count: 0,
      skipped_count: 1,
      failed_count: 1,
    });
    renderModal();

    selectFiles([makeFile('a.png'), makeFile('b.png')]);
    fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));

    expect(await screen.findByText('跳过（内容重复） 1')).toBeInTheDocument();
    expect(screen.getByText('失败 1')).toBeInTheDocument();
    expect(screen.getByText(
      '名字重复：同一路径已存在不同内容的图片'
    )).toBeInTheDocument();
  });
});
