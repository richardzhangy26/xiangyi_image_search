import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ImageAssetRenameError } from '../services/productApi';
import { AssetDisplayNameEditor } from './AssetDisplayNameEditor';

const asset = {
  asset_id: 'asset-1',
  model_number: null,
  display_name: '旧名称.JPG',
  source_relative_path: '目录/旧名称.JPG',
  version: 1,
  status: 'active' as const,
  archived_at: null,
  preview_url: '/api/image-assets/asset-1/preview',
  source_size: 42,
  source_mime_type: 'image/jpeg',
  source_width: 20,
  source_height: 10,
  created_at: null,
};

describe('AssetDisplayNameEditor', () => {
  it('does not apply a UTF-16 maxLength that rejects valid Unicode code points', () => {
    render(<AssetDisplayNameEditor asset={asset} />);
    fireEvent.click(screen.getByRole('button', {
      name: '编辑显示名称 旧名称.JPG',
    }));

    expect(screen.getByRole('textbox', { name: '显示名称主体' }))
      .not.toHaveAttribute('maxlength');
  });

  it('keeps edit explicit and saves with Enter while blur preserves the draft', async () => {
    const renamed = { ...asset, display_name: '新名称.JPG', version: 2 };
    const rename = vi.fn().mockResolvedValue(renamed);
    const onRenamed = vi.fn();
    render(
      <AssetDisplayNameEditor
        asset={asset}
        renameAsset={rename}
        onRenamed={onRenamed}
      />
    );

    expect(screen.getByText('旧名称.JPG')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '编辑显示名称 旧名称.JPG' }))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '编辑显示名称 旧名称.JPG' }));
    const input = screen.getByRole('textbox', { name: '显示名称主体' });
    fireEvent.change(input, { target: { value: '新名称' } });
    fireEvent.blur(input);
    expect(rename).not.toHaveBeenCalled();
    expect(input).toHaveValue('新名称');

    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    await waitFor(() => expect(rename).toHaveBeenCalledWith('asset-1', '新名称', 1));
    expect(onRenamed).toHaveBeenCalledWith(renamed);
    expect(await screen.findByText('新名称.JPG')).toBeInTheDocument();
  });

  it('cancels with Escape without saving', () => {
    const rename = vi.fn();
    render(<AssetDisplayNameEditor asset={asset} renameAsset={rename} />);

    fireEvent.click(screen.getByRole('button', { name: '编辑显示名称 旧名称.JPG' }));
    const input = screen.getByRole('textbox', { name: '显示名称主体' });
    fireEvent.change(input, { target: { value: '放弃草稿' } });
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' });

    expect(rename).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox', { name: '显示名称主体' })).not.toBeInTheDocument();
    expect(screen.getByText('旧名称.JPG')).toBeInTheDocument();
  });

  it('keeps the draft and retries a conflict with the latest version', async () => {
    const latest = { ...asset, display_name: '服务器最新.JPG', version: 2 };
    const renamed = { ...latest, display_name: '用户草稿.JPG', version: 3 };
    const rename = vi.fn()
      .mockRejectedValueOnce(new ImageAssetRenameError(
        '名称已经更新', 409, 'IMAGE_ASSET_VERSION_CONFLICT', latest
      ))
      .mockResolvedValueOnce(renamed);
    render(<AssetDisplayNameEditor asset={asset} renameAsset={rename} />);

    fireEvent.click(screen.getByRole('button', { name: '编辑显示名称 旧名称.JPG' }));
    const input = screen.getByRole('textbox', { name: '显示名称主体' });
    fireEvent.change(input, { target: { value: '用户草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '保存显示名称' }));

    expect(await screen.findByText(/服务器最新名称：服务器最新\.JPG/)).toBeInTheDocument();
    expect(input).toHaveValue('用户草稿');
    fireEvent.click(screen.getByRole('button', { name: '保存显示名称' }));

    await waitFor(() => expect(rename).toHaveBeenLastCalledWith(
      'asset-1', '用户草稿', 2
    ));
    expect(await screen.findByText('用户草稿.JPG')).toBeInTheDocument();
  });

  it('shows a normal failure without losing the draft', async () => {
    const rename = vi.fn().mockRejectedValue(new Error('服务暂不可用'));
    render(<AssetDisplayNameEditor asset={asset} renameAsset={rename} />);

    fireEvent.click(screen.getByRole('button', { name: '编辑显示名称 旧名称.JPG' }));
    const input = screen.getByRole('textbox', { name: '显示名称主体' });
    fireEvent.change(input, { target: { value: '保留草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '保存显示名称' }));

    expect(await screen.findByText('服务暂不可用')).toBeInTheDocument();
    expect(input).toHaveValue('保留草稿');
  });
});
