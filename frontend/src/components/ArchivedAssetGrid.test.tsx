import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ArchivedAssetGrid } from './ArchivedAssetGrid';

interface ArchivedAsset {
  asset_id: string;
  model_number: string | null;
  display_name: string;
  source_relative_path: string;
  version: number;
  status: 'archived';
  archived_at: string;
  preview_url: string;
  source_size: number;
  source_mime_type: string;
  source_width: number;
  source_height: number;
  created_at: string | null;
}

interface ArchivedAssetGridProps {
  assets: ArchivedAsset[];
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

const archivedAsset: ArchivedAsset = {
  asset_id: 'archived-1',
  model_number: null,
  display_name: '蓝色挂绳.png',
  source_relative_path: '挂绳/A47/蓝色/2.png',
  version: 3,
  status: 'archived',
  archived_at: '2026-08-09T12:00:00',
  preview_url: '/api/image-assets/archived-1/preview',
  source_size: 58_896_865,
  source_mime_type: 'image/png',
  source_width: 6000,
  source_height: 4000,
  created_at: '2026-08-02T11:30:00',
};

const secondArchivedAsset: ArchivedAsset = {
  ...archivedAsset,
  asset_id: 'archived-2',
  display_name: '红色挂绳.png',
  source_relative_path: '挂绳/A47/红色/3.png',
  preview_url: '/api/image-assets/archived-2/preview',
};

const assignedArchivedAsset: ArchivedAsset = {
  ...archivedAsset,
  asset_id: 'archived-assigned',
  model_number: 'CS-001',
  display_name: '已归款挂绳.png',
  source_relative_path: '挂绳/CS-001/4.png',
  preview_url: '/api/image-assets/archived-assigned/preview',
};

const baseProps: ArchivedAssetGridProps = {
  assets: [archivedAsset],
  total: 49,
  page: 1,
  pageSize: 24,
  loading: false,
  error: null,
  search: '',
  selectedAssetIds: [],
  restoring: false,
  onSearch: vi.fn(),
  onPageChange: vi.fn(),
  onSelectionChange: vi.fn(),
  onRestore: vi.fn(),
  onRetry: vi.fn(),
};

describe('ArchivedAssetGrid', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows a private preview and read-only archived asset details', () => {
    render(<ArchivedAssetGrid {...baseProps} />);

    expect(screen.getByRole('img', { name: '蓝色挂绳.png' })).toHaveAttribute(
      'src', expect.stringContaining('/api/image-assets/archived-1/preview')
    );
    expect(screen.getByText('蓝色挂绳.png')).toBeInTheDocument();
    expect(screen.getByText('挂绳/A47/蓝色/2.png')).toBeInTheDocument();
    expect(document.body).toHaveTextContent(/归档(?:时间|于).*2026/);
    expect(screen.queryByRole('button', { name: /编辑显示名称/ }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '显示名称主体' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', {
      name: /永久清除|彻底删除|清空回收站/,
    })).not.toBeInTheDocument();
  });

  it('submits display-name-or-path search and archived pagination', () => {
    const onSearch = vi.fn();
    const onPageChange = vi.fn();
    render(
      <ArchivedAssetGrid
        {...baseProps}
        onSearch={onSearch}
        onPageChange={onPageChange}
      />
    );

    expect(document.body).toHaveTextContent(/显示名称.*来源路径/);
    const input = screen.getByPlaceholderText('搜索显示名称或来源路径');
    fireEvent.change(input, { target: { value: '蓝色 挂绳' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    expect(onSearch).toHaveBeenCalledWith('蓝色 挂绳');

    fireEvent.click(screen.getByTitle('2'));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it('restores multiple selected unassigned assets and disables assigned ones', () => {
    const onSelectionChange = vi.fn();
    const onRestore = vi.fn();
    render(
      <ArchivedAssetGrid
        {...baseProps}
        assets={[
          archivedAsset,
          secondArchivedAsset,
          assignedArchivedAsset,
        ]}
        selectedAssetIds={['archived-1']}
        onSelectionChange={onSelectionChange}
        onRestore={onRestore}
      />
    );

    fireEvent.click(screen.getByRole('checkbox', {
      name: '选择 红色挂绳.png',
    }));
    expect(onSelectionChange).toHaveBeenCalledWith([
      'archived-1',
      'archived-2',
    ]);
    expect(screen.getByRole('checkbox', {
      name: '选择 已归款挂绳.png',
    })).toBeDisabled();
    expect(document.body).toHaveTextContent('CS-001');
    expect(document.body).toHaveTextContent(/已归款.*不可恢复/);

    fireEvent.click(screen.getByRole('button', { name: '恢复选中图片' }));
    expect(onRestore).toHaveBeenCalledTimes(1);
  });

  it('locks search, selection and pagination while a restore is in flight', () => {
    render(
      <ArchivedAssetGrid
        {...baseProps}
        selectedAssetIds={['archived-1']}
        restoring
      />
    );

    expect(screen.getByPlaceholderText('搜索显示名称或来源路径'))
      .toBeDisabled();
    expect(screen.getByRole('checkbox', { name: '选择 蓝色挂绳.png' }))
      .toBeDisabled();
    expect(screen.getByTitle('2').closest('ul'))
      .toHaveClass('ant-pagination-disabled');
    expect(screen.getByRole('button', { name: '恢复选中图片' }))
      .toBeDisabled();
  });
});
