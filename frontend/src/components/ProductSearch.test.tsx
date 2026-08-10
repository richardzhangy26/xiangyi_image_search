import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../services/productApi';
import * as imagePreparation from '../utils/prepareSearchImage';
import { ProductSearch } from './ProductSearch';

vi.mock('../services/productApi', async () => {
  const actual = await vi.importActual('../services/productApi');
  return {
    ...actual,
    searchProductsByImage: vi.fn(),
  };
});

vi.mock('../utils/prepareSearchImage', () => ({
  prepareSearchImage: vi.fn(),
}));

describe('ProductSearch result identity', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:query'),
      revokeObjectURL: vi.fn(),
    });
  });

  it('shows display name first and immutable source path second', async () => {
    const file = new File(['image'], 'query.png', { type: 'image/png' });
    vi.mocked(imagePreparation.prepareSearchImage).mockResolvedValue(file);
    vi.mocked(api.searchProductsByImage).mockResolvedValue([{
      asset_id: 'asset-1',
      model_number: null,
      display_name: '客户展示名.PNG',
      source_relative_path: '原始目录/IMG_0001.PNG',
      relative_path: '原始目录/IMG_0001.PNG',
      version: 3,
      preview_url: '/api/image-assets/asset-1/preview',
      similarity: 0.91,
    }]);
    const { container } = render(<ProductSearch />);

    const input = container.querySelector('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '搜索相似产品' }));

    await waitFor(() => expect(api.searchProductsByImage).toHaveBeenCalled());
    expect(screen.getByRole('img', { name: '客户展示名.PNG' }))
      .toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '客户展示名.PNG' }))
      .toBeInTheDocument();
    expect(screen.getByText('型号：未归款')).toBeInTheDocument();
    expect(screen.getByText('原始目录/IMG_0001.PNG')).toBeInTheDocument();
  });
});
