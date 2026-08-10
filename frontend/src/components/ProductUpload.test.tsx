import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProductUpload } from './ProductUpload';
import * as api from '../services/productApi';

vi.mock('../services/productApi', () => ({
  getProducts: vi.fn(),
  getImageAssets: vi.fn(),
  assignImageAssets: vi.fn(),
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
}));

const assetResponse = {
  assets: [{
    asset_id: 'asset-1',
    model_number: null,
    source_relative_path: '手机挂绳/A47/修改后/2.png',
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

describe('ProductUpload unified management view', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [], total: 0, page: 0, per_page: 20,
    });
    vi.mocked(api.getImageAssets).mockResolvedValue(assetResponse);
  });

  it('defaults to real unassigned assets when there are no products', async () => {
    render(<ProductUpload />);

    expect(await screen.findByText(
      '手机挂绳/A47/修改后/2.png'
    )).toBeInTheDocument();
    expect(screen.getByText('2,419 张待归款图片')).toBeInTheDocument();
    expect(screen.getByText('0 个产品')).toBeInTheDocument();
    const workbench = within(screen.getByRole('region', { name: '待归款图片' }));
    expect(workbench.getByRole('button', { name: '关联型号' })).toBeDisabled();
    expect(workbench.getByRole('button', { name: '添加产品' })).toBeEnabled();
    expect(api.getImageAssets).toHaveBeenCalledWith({
      page: 1, perPage: 24, search: '',
    });
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
      screen.getByRole('region', { name: '待归款图片' })
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
      uploaded_images: 0, reused_images: 0, skipped_duplicates: [],
      image_results: [],
    });
    render(<ProductUpload />);

    await screen.findByText('手机挂绳/A47/修改后/2.png');
    fireEvent.click(within(
      screen.getByRole('region', { name: '待归款图片' })
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
});
