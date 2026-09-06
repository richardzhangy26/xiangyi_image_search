import { describe, expect, it } from 'vitest';
import {
  candidatesFromClipboard,
  candidatesFromDataTransfer,
  candidatesFromFileList,
  isImageFileName,
} from './collectImportFiles';

const makeFile = (
  name: string,
  options: { type?: string; webkitRelativePath?: string } = {}
): File => {
  const file = new File(['x'], name, { type: options.type ?? 'image/png' });
  if (options.webkitRelativePath !== undefined) {
    Object.defineProperty(file, 'webkitRelativePath', {
      value: options.webkitRelativePath,
    });
  }
  return file;
};

describe('isImageFileName', () => {
  it('accepts supported extensions case-insensitively', () => {
    expect(isImageFileName('a.png')).toBe(true);
    expect(isImageFileName('a.JPG')).toBe(true);
    expect(isImageFileName('嵌套/目录/a.WEBP')).toBe(true);
  });

  it('rejects unsupported extensions', () => {
    expect(isImageFileName('a.txt')).toBe(false);
    expect(isImageFileName('a')).toBe(false);
  });
});

describe('candidatesFromFileList', () => {
  it('keeps nested folder paths from directory inputs', () => {
    const { candidates, skippedNonImageCount } = candidatesFromFileList([
      makeFile('2.png', { webkitRelativePath: '手机挂绳/A47/修改后/2.png' }),
      makeFile('说明.txt', { webkitRelativePath: '手机挂绳/说明.txt' }),
    ]);

    expect(skippedNonImageCount).toBe(1);
    expect(candidates).toHaveLength(1);
    expect(candidates[0].relativePath).toBe('手机挂绳/A47/修改后/2.png');
    expect(candidates[0].source).toBe('folder');
  });

  it('falls back to the file name for plain multi-select', () => {
    const { candidates } = candidatesFromFileList([makeFile('a.jpg')]);

    expect(candidates[0].relativePath).toBe('a.jpg');
    expect(candidates[0].source).toBe('files');
  });
});

describe('candidatesFromClipboard', () => {
  it('generates deterministic names with date stamp and sequence', () => {
    const { candidates } = candidatesFromClipboard(
      [
        new File(['x'], 'image.png', { type: 'image/png' }),
        new File(['x'], 'image.jpg', { type: 'image/jpeg' }),
      ],
      2
    );

    const stamp = '\\d{8}';
    expect(candidates).toHaveLength(2);
    expect(candidates[0].relativePath).toMatch(
      new RegExp(`^粘贴图片-${stamp}-3\\.png$`)
    );
    expect(candidates[1].relativePath).toMatch(
      new RegExp(`^粘贴图片-${stamp}-4\\.jpg$`)
    );
    expect(candidates[0].source).toBe('clipboard');
  });

  it('skips non-image clipboard payloads', () => {
    const { candidates, skippedNonImageCount } = candidatesFromClipboard([
      new File(['x'], 'note.txt', { type: 'text/plain' }),
    ]);

    expect(candidates).toHaveLength(0);
    expect(skippedNonImageCount).toBe(1);
  });
});

describe('candidatesFromDataTransfer', () => {
  it('falls back to plain files when entries are unavailable', async () => {
    const dataTransfer = {
      items: [],
      files: [makeFile('drop.png'), makeFile('drop.txt')],
    } as unknown as DataTransfer;

    const { candidates, skippedNonImageCount } = await candidatesFromDataTransfer(
      dataTransfer
    );

    expect(skippedNonImageCount).toBe(1);
    expect(candidates).toHaveLength(1);
    expect(candidates[0].relativePath).toBe('drop.png');
    expect(candidates[0].source).toBe('drop');
  });

  it('recurses through nested directory entries', async () => {
    const fileA = makeFile('a.png');
    const fileEntry = {
      isFile: true,
      isDirectory: false,
      name: 'a.png',
      file: (success: (file: File) => void) => success(fileA),
    };
    const directoryEntry = {
      isFile: false,
      isDirectory: true,
      name: '外层',
      createReader: () => ({
        readEntries: (() => {
          let done = false;
          return (success: (entries: unknown[]) => void) => {
            if (done) {
              success([]);
              return;
            }
            done = true;
            success([fileEntry]);
          };
        })(),
      }),
    };
    const dataTransfer = {
      items: [{ webkitGetAsEntry: () => directoryEntry }],
      files: [],
    } as unknown as DataTransfer;

    const { candidates } = await candidatesFromDataTransfer(dataTransfer);

    expect(candidates).toHaveLength(1);
    expect(candidates[0].relativePath).toBe('外层/a.png');
    expect(candidates[0].source).toBe('drop');
  });
});
