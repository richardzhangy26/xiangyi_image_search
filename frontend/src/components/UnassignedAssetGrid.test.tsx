import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UnassignedAssetGrid } from './UnassignedAssetGrid';

const asset = {
  asset_id: 'asset-1',
  model_number: null,
  source_relative_path: '中文 空格/大图.png',
  preview_url: '/api/image-assets/asset-1/preview',
  source_size: 58_896_865,
  source_mime_type: 'image/png',
  source_width: 6000,
  source_height: 4000,
  created_at: '2026-08-02T11:30:00',
};

const baseProps = {
  assets: [asset],
  total: 2419,
  page: 1,
  pageSize: 24,
  loading: false,
  error: null,
  search: '',
  selectedAssetIds: [] as string[],
  canAssign: false,
  onSearch: vi.fn(),
  onPageChange: vi.fn(),
  onSelectionChange: vi.fn(),
  onAssign: vi.fn(),
  onRetry: vi.fn(),
};

describe('UnassignedAssetGrid', () => {
  it('shows path, dimensions, size and private preview', () => {
    render(<UnassignedAssetGrid {...baseProps} />);

    expect(screen.getByText('中文 空格/大图.png')).toBeInTheDocument();
    expect(screen.getByText('6000 × 4000')).toBeInTheDocument();
    expect(screen.getByText('56.2 MB')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '中文 空格/大图.png' })).toHaveAttribute(
      'src', expect.stringContaining('/api/image-assets/asset-1/preview')
    );
  });

  it('selects a card and disables assignment when no product exists', () => {
    const onSelectionChange = vi.fn();
    render(
      <UnassignedAssetGrid
        {...baseProps}
        onSelectionChange={onSelectionChange}
      />
    );

    fireEvent.click(screen.getByRole('checkbox'));
    expect(onSelectionChange).toHaveBeenCalledWith(['asset-1']);
    expect(screen.getByRole('button', { name: '关联型号' })).toBeDisabled();
  });

  it('submits path search and page changes', () => {
    const onSearch = vi.fn();
    const onPageChange = vi.fn();
    render(
      <UnassignedAssetGrid
        {...baseProps}
        onSearch={onSearch}
        onPageChange={onPageChange}
      />
    );

    const input = screen.getByPlaceholderText('搜索来源路径');
    fireEvent.change(input, { target: { value: '中文 空格' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    expect(onSearch).toHaveBeenCalledWith('中文 空格');
    fireEvent.click(screen.getByTitle('2'));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });
});
