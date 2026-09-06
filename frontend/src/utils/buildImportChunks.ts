/**
 * 导入分块：把待导入文件切成后端可接受的小批次。
 * 每块 ≤20 张且 ≤12MiB，避开 Flask 16MiB 与 nginx 20m 请求体上限。
 */

export interface ImportChunk {
  files: File[];
  paths: string[];
}

export const IMPORT_CHUNK_MAX_FILES = 20;
export const IMPORT_CHUNK_MAX_BYTES = 12 * 1024 * 1024;

export const buildImportChunks = (
  rows: Array<{ file: File; targetPath: string }>,
  maxFiles: number = IMPORT_CHUNK_MAX_FILES,
  maxBytes: number = IMPORT_CHUNK_MAX_BYTES
): ImportChunk[] => {
  const chunks: ImportChunk[] = [];
  let current: ImportChunk = { files: [], paths: [] };
  let currentBytes = 0;

  const flush = () => {
    if (current.files.length > 0) {
      chunks.push(current);
      current = { files: [], paths: [] };
      currentBytes = 0;
    }
  };

  rows.forEach(({ file, targetPath }) => {
    const fits =
      current.files.length < maxFiles
      && currentBytes + file.size <= maxBytes;
    if (current.files.length > 0 && !fits) {
      flush();
    }
    current.files.push(file);
    current.paths.push(targetPath);
    currentBytes += file.size;
  });
  flush();
  return chunks;
};
