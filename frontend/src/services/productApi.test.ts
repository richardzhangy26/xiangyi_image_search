import { afterEach, describe, expect, it, vi } from 'vitest';
import * as productApi from './productApi';
import {
    ImageAssetRenameError,
    ProductImageWriteError,
    archiveImageAssets,
    assignImageAssets,
    createImageImports,
    importImageAssets,
    createProduct,
  getImageImportItem,
  getImageImportItems,
  getImageAssets,
  renameImageAsset,
  retryImageImportItem,
  cancelImageImportItem,
  cancelImageImportItems,
  restoreImageImportItem,
  abandonImageImportItem,
} from './productApi';

interface RecycleBinTransportApi {
  getArchivedImageAssets: (params: {
    page: number;
    perPage: number;
    search?: string;
  }) => Promise<unknown>;
  restoreImageAssets: (assetIds: string[]) => Promise<unknown>;
}

const recycleBinApi = productApi as typeof productApi & RecycleBinTransportApi;

afterEach(() => vi.unstubAllGlobals());

describe('image asset management API', () => {
  it('preserves the complete synchronous source-identity import response', async () => {
    const response = {
      items: [{
        relative_path: '手动导入/a.png',
        status: 'in_recycle_bin' as const,
        asset_id: 'archived-18',
        error: null,
        recovery_action: {
          type: 'open_recycle_bin' as const,
          asset_id: 'archived-18',
        },
      }],
      created_count: 0,
      existing_count: 0,
      conflict_count: 0,
      recycle_bin_count: 1,
      failed_count: 0,
      skipped_count: 0,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => response,
    });
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['image'], 'a.png', { type: 'image/png' });

    await expect(importImageAssets([file], ['a.png'], '手动导入'))
      .resolves.toEqual(response);

    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.getAll('images')).toEqual([file]);
    expect(body.get('relative_paths')).toBe(JSON.stringify(['a.png']));
    expect(body.get('prefix')).toBe('手动导入');
    expect(response.skipped_count).toBe(0);
  });

  it.each([400, 413])(
    'throws a typed synchronous import error for HTTP %s',
    async (status) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({
          error: `导入请求被拒绝 (${status})`,
          error_code: 'IMAGE_IMPORT_REJECTED',
        }),
      }));

      const error = await importImageAssets([
        new File(['image'], 'a.png', { type: 'image/png' }),
      ], ['a.png'], '手动导入').catch((caught: unknown) => caught);

      expect(error).toMatchObject({
        name: 'ImageAssetImportRequestError',
        status,
        errorCode: 'IMAGE_IMPORT_REJECTED',
        retryable: false,
      });
      expect(error).toBeInstanceOf(Error);
    }
  );

  it('converts a rejected import fetch into a typed network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    const error = await importImageAssets([
      new File(['image'], 'a.png', { type: 'image/png' }),
    ], ['a.png'], '手动导入').catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      name: 'ImageAssetImportRequestError',
      status: null,
      errorCode: null,
      retryable: true,
    });
    expect(error).toBeInstanceOf(Error);
  });

  it('marks a typed 503 import error as retryable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        error: '图片导入服务暂不可用',
        error_code: 'UPSTREAM_UNAVAILABLE',
      }),
    }));

    const error = await importImageAssets([
      new File(['image'], 'a.png', { type: 'image/png' }),
    ], ['a.png'], '手动导入').catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      name: 'ImageAssetImportRequestError',
      status: 503,
      errorCode: 'UPSTREAM_UNAVAILABLE',
      retryable: true,
    });
  });

  it.each([
    ['TypeError', new TypeError('terminated')],
    ['AbortError', Object.assign(new Error('request aborted'), { name: 'AbortError' })],
  ])('treats a successful response body %s as a retryable interruption', async (_, bodyError) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockRejectedValue(bodyError),
    }));

    const error = await importImageAssets([
      new File(['image'], 'a.png', { type: 'image/png' }),
    ], ['a.png'], '手动导入').catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      name: 'ImageAssetImportRequestError',
      status: null,
      errorCode: null,
      retryable: true,
      message: '图片导入响应读取失败，请稍后重试',
    });
    expect((error as Error).message).not.toContain(bodyError.message);
  });

  it('treats invalid JSON in a successful response as terminal', async () => {
    const parserError = new SyntaxError('Unexpected token at position 0');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockRejectedValue(parserError),
    }));

    const error = await importImageAssets([
      new File(['image'], 'a.png', { type: 'image/png' }),
    ], ['a.png'], '手动导入').catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      name: 'ImageAssetImportRequestError',
      status: 200,
      errorCode: null,
      retryable: false,
      message: '图片导入响应格式无效',
    });
    expect((error as Error).message).not.toContain(parserError.message);
  });

  it('posts standalone image files to the persistent import endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({
        queued_count: 1,
        items: [{
          item_id: 'task-19',
          asset_id: null,
          source_relative_path: 'imports/hash/0001/item.png',
          status: 'queued',
          recovery_action: null,
        }],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['image'], 'item.png', { type: 'image/png' });

    const result = await createImageImports([file]);

    expect(result.queued_count).toBe(1);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/image-imports$/),
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) })
    );
    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.getAll('images')).toEqual([file]);
  });

  it('loads persisted import list and detail with encoded pagination and id', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          items: [], total: 0, page: 2, per_page: 15,
          unresolved_count: 3, processing_count: 2,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          item_id: 'task/19', display_name: 'item.png',
          source_relative_path: 'imports/hash/0001/item.png',
          source_revision: 1, status: 'completed', asset_id: 'asset-19',
          failure_message: null, created_at: null, updated_at: null,
          embedding_started_at: null, completed_at: null, failed_at: null,
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    const list = await getImageImportItems({ page: 2, perPage: 15 });
    const detail = await getImageImportItem('task/19');

    expect(list.unresolved_count).toBe(3);
    expect(detail.asset_id).toBe('asset-19');
    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/image-imports\?page=2&per_page=15$/
    );
    expect(fetchMock.mock.calls[1][0]).toMatch(
      /\/api\/image-imports\/task%2F19$/
    );
  });

  it('posts manual retry to the per-item retry endpoint and parses the item', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        item_id: 'task-20', display_name: 'item.png',
        source_relative_path: 'imports/hash/0001/item.png',
        source_revision: 1, status: 'awaiting_retry', asset_id: null,
        failure_message: null, attempt_count: 3, max_auto_attempts: 5,
        last_error_class: 'rate_limited',
        last_attempt_at: null, next_retry_at: '2026-08-10T12:00:00',
        created_at: null, updated_at: null, embedding_started_at: null,
        completed_at: null, failed_at: null,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await retryImageImportItem('task/20');

    expect(result.status).toBe('awaiting_retry');
    expect(result.next_retry_at).toBe('2026-08-10T12:00:00');
    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/image-imports\/task%2F20\/retry$/
    );
    expect(fetchMock.mock.calls[0][1]).toEqual(
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('surfaces only the safe server message when manual retry is rejected', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '该导入已形成正式资产，无需重试',
        error_code: 'IMAGE_IMPORT_RETRY_COMPLETED',
      }),
    }));

    await expect(retryImageImportItem('task-20'))
      .rejects.toThrow('该导入已形成正式资产，无需重试');
  });

  it('posts single and batch cancel and parses per-item results', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          item_id: 'task-21', display_name: 'item.png',
          source_relative_path: 'imports/hash/0001/item.png',
          source_revision: 1, status: 'cancelled', asset_id: null,
          failure_message: null, cancel_requested_at: '2026-08-10T12:00:00',
          cancelled_at: '2026-08-10T12:00:05', created_at: null,
          updated_at: null, embedding_started_at: null, completed_at: null,
          failed_at: null,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          items: [
            { item_id: 'task-21', result: 'cancelled' },
            { item_id: 'task-22', result: 'completed_rejected' },
          ],
          cancelled_count: 1,
          batch_id: 'batch-21',
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    const single = await cancelImageImportItem('task/21');
    expect(single.status).toBe('cancelled');
    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/image-imports\/task%2F21\/cancel$/
    );

    const batch = await cancelImageImportItems(['task-21', 'task-22']);
    expect(batch.cancelled_count).toBe(1);
    expect(batch.items).toHaveLength(2);
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/image-imports\/cancel$/);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      item_ids: ['task-21', 'task-22'],
    });
  });

  it('surfaces only the safe server message when cancel is rejected', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '该导入已形成正式资产，不能取消',
        error_code: 'IMAGE_IMPORT_CANCEL_COMPLETED',
      }),
    }));

    await expect(cancelImageImportItem('task-21'))
      .rejects.toThrow('该导入已形成正式资产，不能取消');
  });

  it('posts restore and abandon to the per-item endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          item_id: 'task-22', status: 'queued',
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          item_id: 'task-22', status: 'abandoned',
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    const restored = await restoreImageImportItem('task/22');
    expect(restored.status).toBe('queued');
    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/image-imports\/task%2F22\/restore$/
    );

    const abandoned = await abandonImageImportItem('task/22');
    expect(abandoned.status).toBe('abandoned');
    expect(fetchMock.mock.calls[1][0]).toMatch(
      /\/api\/image-imports\/task%2F22\/abandon$/
    );
  });

  it('surfaces window-expiry errors from restore', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 410,
      json: async () => ({
        error: '保留窗口已过，无法恢复',
        error_code: 'IMAGE_IMPORT_RESTORE_WINDOW_EXPIRED',
      }),
    }));

    await expect(restoreImageImportItem('task-22'))
      .rejects.toThrow('保留窗口已过，无法恢复');
  });

  it('uses only the safe server message when import creation fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '来源冲突，未覆盖现有内容',
        provider_body: 'must-not-surface',
      }),
    }));

    await expect(createImageImports([
      new File(['image'], 'item.png', { type: 'image/png' }),
    ])).rejects.toThrow('来源冲突，未覆盖现有内容');
  });

  it('preserves the dedicated source-conflict result for product uploads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '来源冲突：同一来源身份已存在不同内容，未覆盖现有资产',
        error_code: 'IMAGE_ASSET_SOURCE_CONFLICT',
        image_results: [{
          asset_id: 'asset-existing',
          source_relative_path: 'catalog/item.png',
          status: 'source_conflict',
        }],
      }),
    }));

    const error = await createProduct({
      model_number: 'MODEL-18',
      photographer_file: 'photo',
      alibaba_product_url: 'https://example.test/item',
      category: '挂绳',
    }, []).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ProductImageWriteError);
    expect((error as ProductImageWriteError).status).toBe(409);
    expect((error as ProductImageWriteError).errorCode)
      .toBe('IMAGE_ASSET_SOURCE_CONFLICT');
    expect((error as ProductImageWriteError).imageResults).toEqual([{
      asset_id: 'asset-existing',
      source_relative_path: 'catalog/item.png',
      status: 'source_conflict',
    }]);
  });

  it('requests one filtered archived page with the public query contract', async () => {
    const response = {
      assets: [], total: 0, archived_total: 37, page: 2, per_page: 24,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => response,
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(recycleBinApi.getArchivedImageAssets({
      page: 2,
      perPage: 24,
      search: '中文 空格',
    })).resolves.toEqual(response);

    expect(fetchMock).toHaveBeenCalledWith(expect.any(String), {
      method: 'GET',
    });
    const requestUrl = new URL(String(fetchMock.mock.calls[0][0]), 'http://local.test');
    expect(requestUrl.pathname).toBe('/api/image-assets/archived');
    expect(Object.fromEntries(requestUrl.searchParams)).toEqual({
      page: '2',
      per_page: '24',
      search: '中文 空格',
    });
  });

  it('posts selected ids to the atomic restore endpoint', async () => {
    const response = {
      batch_id: 'restore-batch-1', status: 'succeeded' as const,
      restored_count: 2, already_active_count: 0, items: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => response,
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(recycleBinApi.restoreImageAssets(['asset-1', 'asset-2']))
      .resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/restore'),
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_ids: ['asset-1', 'asset-2'] }),
      })
    );
  });

  it('summarizes every readable item error from a rejected restore', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '本批图片无法恢复',
        items: [
          {
            asset_id: 'asset-1', status: 'rejected',
            error: '蓝色挂绳.png 已关联型号，无法恢复',
          },
          {
            asset_id: 'asset-2', status: 'rejected',
            error: '红色挂绳.png 当前状态不允许恢复',
          },
        ],
      }),
    }));

    const error = await recycleBinApi.restoreImageAssets(['asset-1', 'asset-2'])
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain(
      '蓝色挂绳.png 已关联型号，无法恢复'
    );
    expect((error as Error).message).toContain(
      '红色挂绳.png 当前状态不允许恢复'
    );
  });

  it('posts selected ids to the atomic archive endpoint', async () => {
    const response = {
      batch_id: 'batch-1', status: 'succeeded' as const,
      archived_count: 2, already_archived_count: 0, items: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => response,
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(archiveImageAssets(['asset-1', 'asset-2']))
      .resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/archive'),
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_ids: ['asset-1', 'asset-2'] }),
      })
    );
  });

  it('sends the admin bearer token only to purge readiness', async () => {
    const payload = {
      purge_available: false,
      pipeline_available: false,
      checked_at: '2026-08-22T12:00:00Z',
      conditions: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => payload,
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(productApi.getPurgeReadiness('abc')).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/purge/readiness'),
      expect.objectContaining({
        method: 'GET',
        headers: { Authorization: 'Bearer abc' },
      })
    );
    expect(productApi).not.toHaveProperty('createPurgeBatch');
    expect(productApi).not.toHaveProperty('cancelPurgeBatch');
    expect(productApi).not.toHaveProperty('retryPurgeBatch');
  });

  it('surfaces the backend archive error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ error: '图片已经归款，无法移入回收站' }),
    }));

    await expect(archiveImageAssets(['asset-1'])).rejects.toThrow(
      '图片已经归款，无法移入回收站'
    );
  });

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

  it('posts the editable body with the expected version', async () => {
    const asset = {
      asset_id: 'asset-1', model_number: null, display_name: '新名称.JPG',
      source_relative_path: '目录/旧名称.JPG', version: 4, status: 'active' as const,
      preview_url: '/api/image-assets/asset-1/preview', source_size: 1,
      source_mime_type: 'image/jpeg', source_width: 1, source_height: 1,
      created_at: null,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ asset }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(renameImageAsset('asset-1', '新名称', 3)).resolves.toEqual(asset);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/asset-1/rename'),
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name_body: '新名称', expected_version: 3 }),
      })
    );
  });

  it('preserves the latest representation on a version conflict', async () => {
    const latest = {
      asset_id: 'asset-1', model_number: 'CS-001', display_name: '服务器最新.JPG',
      source_relative_path: '目录/旧名称.JPG', version: 5, status: 'active' as const,
      preview_url: '/api/image-assets/asset-1/preview', source_size: 1,
      source_mime_type: 'image/jpeg', source_width: 1, source_height: 1,
      created_at: null,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: '名称已经更新',
        error_code: 'IMAGE_ASSET_VERSION_CONFLICT',
        latest,
      }),
    }));

    const error = await renameImageAsset('asset-1', '用户草稿', 4)
      .catch((caught) => caught);

    expect(error).toBeInstanceOf(ImageAssetRenameError);
    expect(error.errorCode).toBe('IMAGE_ASSET_VERSION_CONFLICT');
    expect(error.latest).toEqual(latest);
  });
});
