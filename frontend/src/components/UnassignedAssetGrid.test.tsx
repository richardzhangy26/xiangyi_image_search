import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UnassignedAssetGrid } from './UnassignedAssetGrid';

const asset = {
  asset_id: 'asset-1',
  model_number: null,
  display_name: '业务名称.png',
  source_relative_path: '中文 空格/大图.png',
  version: 1,
  status: 'active' as const,
  archived_at: null,
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
  assignment: 'unassigned' as const,
  selectedAssetIds: [] as string[],
  canAssign: false,
  onSearch: vi.fn(),
  onAssignmentChange: vi.fn(),
  onPageChange: vi.fn(),
  onSelectionChange: vi.fn(),
  onAssign: vi.fn(),
  onArchive: vi.fn(),
  onRetry: vi.fn(),
  onAssetRenamed: vi.fn(),
};

describe('UnassignedAssetGrid', () => {
  it('shows display name first, source path second and a permanent edit entry', () => {
    render(<UnassignedAssetGrid {...baseProps} />);

    expect(screen.getByText('业务名称.png')).toBeInTheDocument();
    expect(screen.getByText('中文 空格/大图.png')).toBeInTheDocument();
    expect(screen.getByText('6000 × 4000')).toBeInTheDocument();
    expect(screen.getByText('56.2 MB')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '业务名称.png' })).toHaveAttribute(
      'src', expect.stringContaining('/api/image-assets/asset-1/preview')
    );
    expect(screen.getByRole('button', { name: '编辑显示名称 业务名称.png' }))
      .toBeInTheDocument();
  });

  it('selects a card and disables assignment when no product exists', () => {
    const onSelectionChange = vi.fn();
    render(
      <UnassignedAssetGrid
        {...baseProps}
        selectedAssetIds={['asset-1']}
        onSelectionChange={onSelectionChange}
      />
    );

    fireEvent.click(screen.getByRole('checkbox'));
    expect(onSelectionChange).toHaveBeenCalledWith([]);
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

    const input = screen.getByPlaceholderText('搜索显示名称或来源路径');
    fireEvent.change(input, { target: { value: '中文 空格' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    expect(onSearch).toHaveBeenCalledWith('中文 空格');
    fireEvent.click(screen.getByTitle('2'));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it('switches between unassigned, assigned and all active assets', () => {
    const onAssignmentChange = vi.fn();
    render(
      <UnassignedAssetGrid
        {...baseProps}
        onAssignmentChange={onAssignmentChange}
      />
    );

    fireEvent.click(screen.getByText('已归款'));
    expect(onAssignmentChange).toHaveBeenCalledWith('assigned');
    fireEvent.click(screen.getByText('全部'));
    expect(onAssignmentChange).toHaveBeenCalledWith('all');
  });

  it('keeps an assigned asset checkbox disabled', () => {
    render(
      <UnassignedAssetGrid
        {...baseProps}
        assets={[{ ...asset, model_number: 'CS-001' }]}
      />
    );

    expect(screen.getByRole('checkbox')).toBeDisabled();
  });
});
