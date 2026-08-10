import { afterEach, describe, expect, it, vi } from 'vitest';
import { assignImageAssets, getImageAssets } from './productApi';

afterEach(() => vi.unstubAllGlobals());

describe('image asset management API', () => {
  it('requests one filtered unassigned page', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ assets: [], total: 0, page: 2, per_page: 24 }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await getImageAssets({ page: 2, perPage: 24, search: '中文 空格' });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        '/api/image-assets?assignment=unassigned&page=2&per_page=24&search='
      ),
      { method: 'GET' }
    );
  });

  it('posts selected ids and the real model number', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        model_number: 'CS-001', assigned_count: 2, reused_count: 0,
        product_created: false,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await assignImageAssets(['asset-1', 'asset-2'], 'CS-001');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/assign'),
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asset_ids: ['asset-1', 'asset-2'], model_number: 'CS-001',
          create_if_missing: false,
        }),
      })
    );
  });

  it('requests quick product creation when the model is missing', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        model_number: 'NEW-001', assigned_count: 1, reused_count: 0,
        product_created: true,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await assignImageAssets(['asset-1'], 'NEW-001', {
      createIfMissing: true,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/assign'),
      expect.objectContaining({
        body: JSON.stringify({
          asset_ids: ['asset-1'], model_number: 'NEW-001',
          create_if_missing: true,
        }),
      })
    );
    expect(result.product_created).toBe(true);
  });

  it('surfaces the backend assignment error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ error: '目标型号不存在，请刷新产品列表' }),
    }));

    await expect(assignImageAssets(['asset-1'], 'MISSING')).rejects.toThrow(
      '目标型号不存在，请刷新产品列表'
    );
  });
});
