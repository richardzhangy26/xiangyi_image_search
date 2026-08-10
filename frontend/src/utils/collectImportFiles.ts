/**
 * 本地导入文件采集：统一单图、嵌套文件夹、拖拽与剪贴板来源。
 * 只保留受支持的图片扩展名，并保留文件夹的嵌套相对路径。
 */

export interface ImportCandidate {
  file: File;
  relativePath: string;
  source: 'files' | 'folder' | 'drop' | 'clipboard';
}

export interface CollectResult {
  candidates: ImportCandidate[];
  skippedNonImageCount: number;
}

export const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];

export const isImageFileName = (name: string): boolean => {
  const lower = name.toLowerCase();
  return IMAGE_EXTENSIONS.some((suffix) => lower.endsWith(suffix));
};

/** 目录选择/普通多选中采集候选文件；目录选择保留 webkitRelativePath。 */
export const candidatesFromFileList = (
  files: File[] | FileList,
  fallbackSource: ImportCandidate['source'] = 'files'
): CollectResult => {
  const candidates: ImportCandidate[] = [];
  let skippedNonImageCount = 0;
  Array.from(files).forEach((file) => {
    const relativePath = file.webkitRelativePath || file.name;
    if (!isImageFileName(relativePath)) {
      skippedNonImageCount += 1;
      return;
    }
    candidates.push({
      file,
      relativePath,
      source: file.webkitRelativePath ? 'folder' : fallbackSource,
    });
  });
  return { candidates, skippedNonImageCount };
};

const pasteExtension = (file: File): string => {
  const mime = file.type.toLowerCase();
  if (mime === 'image/jpeg') return '.jpg';
  if (mime === 'image/png') return '.png';
  if (mime === 'image/gif') return '.gif';
  if (mime === 'image/webp') return '.webp';
  return '.png';
};

/** 剪贴板采集：生成会话内确定性文件名，保证失败重试不产生重复对象。 */
export const candidatesFromClipboard = (
  files: File[] | FileList,
  existingCount = 0
): CollectResult => {
  const candidates: ImportCandidate[] = [];
  let skippedNonImageCount = 0;
  const date = new Date();
  const stamp = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}`;
  let sequence = existingCount;
  Array.from(files).forEach((file) => {
    if (!file.type.startsWith('image/')) {
      skippedNonImageCount += 1;
      return;
    }
    sequence += 1;
    candidates.push({
      file,
      relativePath: `粘贴图片-${stamp}-${sequence}${pasteExtension(file)}`,
      source: 'clipboard',
    });
  });
  return { candidates, skippedNonImageCount };
};

interface FileSystemEntryLike {
  readonly isFile: boolean;
  readonly isDirectory: boolean;
  readonly name: string;
}

interface FileSystemFileEntryLike extends FileSystemEntryLike {
  file(success: (file: File) => void, error?: (error: unknown) => void): void;
}

interface FileSystemDirectoryReaderLike {
  readEntries(
    success: (entries: FileSystemEntryLike[]) => void,
    error?: (error: unknown) => void
  ): void;
}

interface FileSystemDirectoryEntryLike extends FileSystemEntryLike {
  createReader(): FileSystemDirectoryReaderLike;
}

const readFileEntry = (entry: FileSystemFileEntryLike): Promise<File> =>
  new Promise((resolve, reject) => {
    entry.file(resolve, reject);
  });

const readDirectoryEntries = (
  reader: FileSystemDirectoryReaderLike
): Promise<FileSystemEntryLike[]> =>
  new Promise((resolve, reject) => {
    reader.readEntries(resolve, reject);
  });

const readEntryRecursive = async (
  entry: FileSystemEntryLike,
  basePath: string,
  sink: ImportCandidate[],
  skipCounter: { count: number }
): Promise<void> => {
  const path = basePath ? `${basePath}/${entry.name}` : entry.name;
  if (entry.isFile) {
    try {
      const file = await readFileEntry(entry as FileSystemFileEntryLike);
      if (!isImageFileName(entry.name)) {
        skipCounter.count += 1;
        return;
      }
      sink.push({ file, relativePath: path, source: 'drop' });
    } catch {
      skipCounter.count += 1;
    }
    return;
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntryLike).createReader();
    // readEntries 单次最多返回 100 项，循环读取直到返回空数组。
    for (;;) {
      const batch = await readDirectoryEntries(reader);
      if (batch.length === 0) break;
      for (const child of batch) {
        await readEntryRecursive(child, path, sink, skipCounter);
      }
    }
  }
};

/** 拖拽采集：优先用 FileSystemEntry 递归遍历嵌套文件夹。 */
export const candidatesFromDataTransfer = async (
  dataTransfer: DataTransfer
): Promise<CollectResult> => {
  const candidates: ImportCandidate[] = [];
  const skipCounter = { count: 0 };
  const items = Array.from(dataTransfer.items || []);
  const entries: FileSystemEntryLike[] = [];
  items.forEach((item) => {
    if (typeof item.webkitGetAsEntry !== 'function') return;
    const entry = item.webkitGetAsEntry();
    if (entry) entries.push(entry as unknown as FileSystemEntryLike);
  });

  if (entries.length > 0) {
    for (const entry of entries) {
      await readEntryRecursive(entry, '', candidates, skipCounter);
    }
    return { candidates, skippedNonImageCount: skipCounter.count };
  }
  return candidatesFromFileList(dataTransfer.files, 'drop');
};
