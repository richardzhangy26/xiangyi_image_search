import { describe, expect, it } from 'vitest';
import type { UploadFile } from 'antd/es/upload/interface';
import { buildImageOrderPayload, moveByUid } from './productImageOrder';

const existing = (uid: string): UploadFile => ({
  uid,
  name: `${uid}.jpg`,
  status: 'done',
});

const fresh = (uid: string): UploadFile => ({
  uid,
  name: `${uid}.jpg`,
  status: 'done',
  originFileObj: new File(['x'], `${uid}.jpg`, { type: 'image/jpeg' }),
});

describe('buildImageOrderPayload', () => {
  it('keeps existing asset ids and places new uploads as new:<index>', () => {
    const { imageFiles, imageOrder } = buildImageOrderPayload([
      existing('asset-a'),
      fresh('rc-1'),
      existing('asset-b'),
      fresh('rc-2'),
    ]);
    expect(imageOrder).toEqual(['asset-a', 'new:0', 'asset-b', 'new:1']);
    expect(imageFiles.map((file) => file.name)).toEqual(['rc-1.jpg', 'rc-2.jpg']);
  });

  it('returns empty payload for empty list', () => {
    expect(buildImageOrderPayload([])).toEqual({ imageFiles: [], imageOrder: [] });
  });
});

describe('moveByUid', () => {
  it('moves the dragged item before the drop target order-wise', () => {
    const result = moveByUid(
      [existing('a'), existing('b'), existing('c')],
      'c',
      'a'
    );
    expect(result.map((item) => item.uid)).toEqual(['c', 'a', 'b']);
  });

  it('returns the original list when uid is missing or unchanged', () => {
    const list = [existing('a'), existing('b')];
    expect(moveByUid(list, 'a', 'zzz')).toBe(list);
    expect(moveByUid(list, 'a', 'a')).toBe(list);
  });
});
