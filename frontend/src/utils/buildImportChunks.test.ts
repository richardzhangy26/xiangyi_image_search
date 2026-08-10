import { describe, expect, it } from 'vitest';
import { buildImportChunks } from './buildImportChunks';

const makeFile = (name: string, size: number): File => {
  const file = new File(['x'], name, { type: 'image/png' });
  Object.defineProperty(file, 'size', { value: size });
  return file;
};

const rows = (specs: Array<[string, number]>) => specs.map(
  ([name, size]) => ({ file: makeFile(name, size), targetPath: name })
);

const MB = 1024 * 1024;

describe('buildImportChunks', () => {
  it('returns no chunks for empty input', () => {
    expect(buildImportChunks([])).toEqual([]);
  });

  it('splits at the 20-file batch limit', () => {
    const chunks = buildImportChunks(
      rows(Array.from({ length: 45 }, (_, index) => [`${index}.png`, 1024]))
    );

    expect(chunks.map((chunk) => chunk.files.length)).toEqual([20, 20, 5]);
    expect(chunks[0].paths[0]).toBe('0.png');
    expect(chunks[2].paths).toEqual(['40.png', '41.png', '42.png', '43.png', '44.png']);
  });

  it('splits at the 12 MiB byte limit', () => {
    const chunks = buildImportChunks(rows([
      ['big-1.png', 7 * MB],
      ['big-2.png', 7 * MB],
      ['small.png', 1 * MB],
    ]));

    expect(chunks).toHaveLength(2);
    expect(chunks[0].paths).toEqual(['big-1.png']);
    expect(chunks[1].paths).toEqual(['big-2.png', 'small.png']);
  });

  it('keeps a single oversized file in its own chunk', () => {
    const chunks = buildImportChunks(rows([
      ['huge.png', 15 * MB],
      ['small.png', 1 * MB],
    ]));

    expect(chunks.map((chunk) => chunk.paths)).toEqual([
      ['huge.png'],
      ['small.png'],
    ]);
  });
});
