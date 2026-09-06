/**
 * 本地导入弹窗：单图 / 嵌套文件夹 / 剪贴板粘贴 → 确认命名 → 分块导入待归款图片。
 * 导入的资产 model_number 为空，可参与以图搜款，不创建产品记录。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Input,
  Modal,
  Progress,
  Tag,
  Tooltip,
  message,
} from 'antd';
import {
  DeleteOutlined,
  FileImageOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  SnippetsOutlined,
} from '@ant-design/icons';
import type { ImageAssetImportItem } from '../types/product';
import {
  ImageAssetImportRequestError,
  importImageAssets,
} from '../services/productApi';
import {
  candidatesFromClipboard,
  candidatesFromDataTransfer,
  candidatesFromFileList,
  type ImportCandidate,
} from '../utils/collectImportFiles';
import { buildImportChunks, type ImportChunk } from '../utils/buildImportChunks';

export const IMPORT_TOTAL_LIMIT = 300;

interface ImportRow extends ImportCandidate {
  id: string;
  targetPath: string;
  previewUrl: string;
}

interface ImportChunkWithRows extends ImportChunk {
  rowIds: string[];
}

interface ImportImagesModalProps {
  open: boolean;
  onClose: () => void;
  onFinished: () => void;
  onOpenRecycleBin: () => void;
}

type ImportPhase = 'edit' | 'upload' | 'done';

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
};

let rowSequence = 0;
const nextRowId = () => {
  rowSequence += 1;
  return `import-row-${rowSequence}`;
};

const STATUS_LABELS: Record<ImageAssetImportItem['status'], {
  text: string;
  color: string;
}> = {
  created: { text: '导入成功', color: 'green' },
  existing: { text: '已存在（幂等）', color: 'blue' },
  source_conflict: { text: '来源冲突', color: 'red' },
  in_recycle_bin: { text: '在回收站', color: 'orange' },
  failed: { text: '失败', color: 'red' },
};

export const ImportImagesModal: React.FC<ImportImagesModalProps> = ({
  open,
  onClose,
  onFinished,
  onOpenRecycleBin,
}) => {
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [prefix, setPrefix] = useState('手动导入');
  const [phase, setPhase] = useState<ImportPhase>('edit');
  const [chunkProgress, setChunkProgress] = useState({ current: 0, total: 0 });
  const [resultsByRowId, setResultsByRowId] = useState<
    Record<string, ImageAssetImportItem>
  >({});
  const [failedChunks, setFailedChunks] = useState<ImportChunkWithRows[]>([]);
  const [skippedNonImage, setSkippedNonImage] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  const addCandidates = (candidates: ImportCandidate[]) => {
    if (candidates.length === 0) return;
    setRows((current) => {
      const merged = [
        ...current,
        ...candidates.map((candidate) => ({
          ...candidate,
          id: nextRowId(),
          targetPath: candidate.relativePath,
          previewUrl: URL.createObjectURL(candidate.file),
        })),
      ];
      if (merged.length > IMPORT_TOTAL_LIMIT) {
        message.warning(
          `单次最多导入 ${IMPORT_TOTAL_LIMIT} 张，请分批操作`
        );
      }
      return merged;
    });
  };

  const handleFileInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const { candidates, skippedNonImageCount } = candidatesFromFileList(
      event.target.files ?? []
    );
    setSkippedNonImage((count) => count + skippedNonImageCount);
    addCandidates(candidates);
    event.target.value = '';
  };

  const handlePaste = (event: React.ClipboardEvent) => {
    if (phase !== 'edit') return;
    const { candidates, skippedNonImageCount } = candidatesFromClipboard(
      event.clipboardData.files,
      rows.length
    );
    if (candidates.length === 0 && skippedNonImageCount === 0) return;
    setSkippedNonImage((count) => count + skippedNonImageCount);
    addCandidates(candidates);
  };

  const handleDrop = async (event: React.DragEvent) => {
    event.preventDefault();
    if (phase !== 'edit') return;
    const { candidates, skippedNonImageCount } = await candidatesFromDataTransfer(
      event.dataTransfer
    );
    setSkippedNonImage((count) => count + skippedNonImageCount);
    addCandidates(candidates);
  };

  const removeRow = (id: string) => {
    setRows((current) => {
      const removed = current.find((row) => row.id === id);
      if (removed) URL.revokeObjectURL(removed.previewUrl);
      return current.filter((row) => row.id !== id);
    });
  };

  const updateTargetPath = (id: string, targetPath: string) => {
    setRows((current) => current.map(
      (row) => (row.id === id ? { ...row, targetPath } : row)
    ));
  };

  const reset = () => {
    rows.forEach((row) => URL.revokeObjectURL(row.previewUrl));
    setRows([]);
    setPrefix('手动导入');
    setPhase('edit');
    setResultsByRowId({});
    setFailedChunks([]);
    setSkippedNonImage(0);
    setChunkProgress({ current: 0, total: 0 });
  };

  useEffect(() => {
    if (!open) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => () => {
    rows.forEach((row) => URL.revokeObjectURL(row.previewUrl));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fullPathByRowId = useMemo(() => {
    const map = new Map<string, string>();
    rows.forEach((row) => {
      map.set(row.id, `${prefix.trim()}/${row.targetPath.trim()}`);
    });
    return map;
  }, [rows, prefix]);

  const duplicateFullPaths = useMemo(() => {
    const counts = new Map<string, number>();
    fullPathByRowId.forEach((fullPath) => {
      counts.set(fullPath, (counts.get(fullPath) ?? 0) + 1);
    });
    const duplicates = new Set<string>();
    counts.forEach((count, fullPath) => {
      if (count > 1) duplicates.add(fullPath);
    });
    return duplicates;
  }, [fullPathByRowId]);

  const invalidRows = useMemo(() => new Set(
    rows
      .filter((row) => !row.targetPath.trim() || row.targetPath.includes('..'))
      .map((row) => row.id)
  ), [rows]);

  const totalBytes = useMemo(
    () => rows.reduce((sum, row) => sum + row.file.size, 0),
    [rows]
  );

  const canSubmit = phase === 'edit'
    && rows.length > 0
    && rows.length <= IMPORT_TOTAL_LIMIT
    && prefix.trim().length > 0
    && duplicateFullPaths.size === 0
    && invalidRows.size === 0;

  const runChunks = async (chunks: ImportChunkWithRows[]) => {
    const aggregated = { ...resultsByRowId };
    const failed: ImportChunkWithRows[] = [];
    for (let index = 0; index < chunks.length; index += 1) {
      const chunk = chunks[index];
      setChunkProgress({ current: index + 1, total: chunks.length });
      try {
        const response = await importImageAssets(
          chunk.files,
          chunk.paths,
          prefix.trim()
        );
        if (response.items.length !== chunk.paths.length) {
          chunk.rowIds.forEach((rowId, itemIndex) => {
            aggregated[rowId] = {
              relative_path: `${prefix.trim()}/${chunk.paths[itemIndex]}`,
              status: 'failed',
              asset_id: null,
              error: '导入结果数量与请求不一致',
              recovery_action: null,
            };
          });
          continue;
        }
        const failedItemChunk: ImportChunkWithRows = {
          files: [], paths: [], rowIds: [],
        };
        response.items.forEach((item, itemIndex) => {
          aggregated[chunk.rowIds[itemIndex]] = item;
          if (item.status === 'failed') {
            failedItemChunk.files.push(chunk.files[itemIndex]);
            failedItemChunk.paths.push(chunk.paths[itemIndex]);
            failedItemChunk.rowIds.push(chunk.rowIds[itemIndex]);
          }
        });
        if (failedItemChunk.files.length > 0) {
          failed.push(failedItemChunk);
        }
      } catch (error) {
        if (
          error instanceof ImageAssetImportRequestError
          && error.retryable
        ) {
          failed.push(chunk);
        }
        const reason = error instanceof Error ? error.message : '导入失败';
        chunk.paths.forEach((path, itemIndex) => {
          aggregated[chunk.rowIds[itemIndex]] = {
            relative_path: `${prefix.trim()}/${path}`,
            status: 'failed',
            asset_id: null,
            error: reason,
            recovery_action: null,
          };
        });
      }
    }
    setResultsByRowId(aggregated);
    setFailedChunks(failed);
    setPhase('done');
    onFinished();
  };

  const startImport = async () => {
    setPhase('upload');
    const importChunks = buildImportChunks(
      rows.map((row) => ({ file: row.file, targetPath: row.targetPath.trim() }))
    );
    let rowOffset = 0;
    const chunks = importChunks.map((chunk) => {
      const rowIds = rows
        .slice(rowOffset, rowOffset + chunk.files.length)
        .map((row) => row.id);
      rowOffset += chunk.files.length;
      return { ...chunk, rowIds };
    });
    await runChunks(chunks);
  };

  const retryFailed = async () => {
    if (failedChunks.length === 0) return;
    setPhase('upload');
    await runChunks(failedChunks);
  };

  const results = useMemo(() => rows.flatMap((row) => {
    const result = resultsByRowId[row.id];
    return result ? [result] : [];
  }), [rows, resultsByRowId]);

  const summary = useMemo(() => {
    const counts = {
      created: 0,
      existing: 0,
      conflict: 0,
      recycleBin: 0,
      failed: 0,
    };
    results.forEach((item) => {
      if (item.status === 'created') counts.created += 1;
      else if (item.status === 'existing') counts.existing += 1;
      else if (item.status === 'source_conflict') counts.conflict += 1;
      else if (item.status === 'in_recycle_bin') counts.recycleBin += 1;
      else counts.failed += 1;
    });
    return counts;
  }, [results]);

  return (
    <Modal
      title="导入图片到待归款"
      open={open}
      onCancel={phase === 'upload' ? undefined : onClose}
      width={820}
      footer={null}
      destroyOnClose
    >
      <div onPaste={handlePaste}>
        {phase === 'edit' && (
          <>
            <div
              className="mb-4 rounded-xl border-2 border-dashed border-slate-300 bg-slate-50 px-6 py-6 text-center transition-colors hover:border-teal-400"
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              <InboxOutlined className="text-3xl text-slate-400" />
              <p className="mt-2 mb-1 text-sm text-slate-600">
                拖拽图片或文件夹到这里，也可以在弹窗内直接粘贴截图
              </p>
              <div className="flex items-center justify-center gap-3">
                <Button
                  icon={<FileImageOutlined />}
                  onClick={() => fileInputRef.current?.click()}
                >
                  选择图片
                </Button>
                <Button
                  icon={<FolderOpenOutlined />}
                  onClick={() => folderInputRef.current?.click()}
                >
                  选择文件夹
                </Button>
              </div>
              <p className="mt-2 mb-0 text-xs text-slate-400">
                <SnippetsOutlined className="mr-1" />
                支持嵌套文件夹；仅接收 png / jpg / jpeg / gif / webp
              </p>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={handleFileInputChange}
            />
            <input
              ref={folderInputRef}
              type="file"
              multiple
              className="hidden"
              // @ts-expect-error 目录选择依赖非标准 webkitdirectory 属性
              webkitdirectory=""
              onChange={handleFileInputChange}
            />

            {rows.length > 0 && (
              <>
                <div className="mb-3 flex items-center gap-3">
                  <span className="text-sm text-slate-600">命名前缀</span>
                  <Input
                    value={prefix}
                    onChange={(event) => setPrefix(event.target.value)}
                    style={{ maxWidth: 220 }}
                    placeholder="手动导入"
                  />
                  <span className="text-xs text-slate-400">
                    已选 {rows.length} 张 · 共 {formatBytes(totalBytes)}
                    {skippedNonImage > 0 && ` · 忽略非图片 ${skippedNonImage} 个`}
                  </span>
                </div>

                {rows.length > IMPORT_TOTAL_LIMIT && (
                  <Alert
                    type="warning"
                    showIcon
                    className="mb-3"
                    message={`单次最多导入 ${IMPORT_TOTAL_LIMIT} 张，请删除部分图片后重试`}
                  />
                )}
                {duplicateFullPaths.size > 0 && (
                  <Alert
                    type="error"
                    showIcon
                    className="mb-3"
                    message="存在重复的目标路径，请修改后再导入"
                  />
                )}

                <div
                  className="overflow-auto rounded-lg border border-slate-200"
                  style={{ maxHeight: 320 }}
                >
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-slate-50 text-left text-xs text-slate-500">
                      <tr>
                        <th className="px-3 py-2">预览</th>
                        <th className="px-3 py-2">目标路径（可编辑）</th>
                        <th className="px-3 py-2">大小</th>
                        <th className="px-3 py-2" />
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => {
                        const fullPath = fullPathByRowId.get(row.id) ?? '';
                        const isDuplicate = duplicateFullPaths.has(fullPath);
                        const isInvalid = invalidRows.has(row.id);
                        return (
                          <tr
                            key={row.id}
                            className="border-t border-slate-100"
                          >
                            <td className="px-3 py-2">
                              <img
                                src={row.previewUrl}
                                alt={row.targetPath}
                                className="h-10 w-10 rounded object-cover"
                              />
                            </td>
                            <td className="px-3 py-2">
                              <Input
                                value={row.targetPath}
                                status={
                                  isDuplicate || isInvalid ? 'error' : undefined
                                }
                                onChange={(event) => updateTargetPath(
                                  row.id,
                                  event.target.value
                                )}
                                addonBefore={prefix.trim() || '无前缀'}
                              />
                              {isDuplicate && (
                                <span className="text-xs text-red-500">
                                  本批路径重复
                                </span>
                              )}
                              {isInvalid && !isDuplicate && (
                                <span className="text-xs text-red-500">
                                  路径无效（不能为空或包含 ..）
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-xs text-slate-500">
                              {formatBytes(row.file.size)}
                            </td>
                            <td className="px-3 py-2 text-right">
                              <Button
                                type="text"
                                danger
                                size="small"
                                icon={<DeleteOutlined />}
                                aria-label={`移除 ${row.targetPath}`}
                                onClick={() => removeRow(row.id)}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {rows.length === 0 && (
              <p className="text-center text-sm text-slate-400">
                还没有选择图片。导入后图片进入待归款列表并可参与以图搜款，不会录入产品资料。
              </p>
            )}

            <div className="mt-5 flex justify-end gap-3">
              <Button onClick={onClose}>取消</Button>
              <Tooltip title={canSubmit ? '' : '请先选择图片并处理路径问题'}>
                <Button type="primary" disabled={!canSubmit} onClick={startImport}>
                  开始导入（{rows.length} 张）
                </Button>
              </Tooltip>
            </div>
          </>
        )}

        {phase === 'upload' && (
          <div className="py-10 text-center">
            <Progress
              percent={
                chunkProgress.total > 0
                  ? Math.round(
                    ((chunkProgress.current - 0.5) / chunkProgress.total) * 100
                  )
                  : 0
              }
              strokeColor={{ '0%': '#0d7a72', '100%': '#d97b29' }}
            />
            <p className="mt-3 text-sm text-slate-500">
              正在导入第 {chunkProgress.current} / {chunkProgress.total} 块，
              请勿关闭页面…
            </p>
          </div>
        )}

        {phase === 'done' && (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Tag color="green">成功 {summary.created}</Tag>
              {summary.existing > 0 && (
                <Tag color="blue">已存在（幂等） {summary.existing}</Tag>
              )}
              {summary.conflict > 0 && (
                <Tag color="red">来源冲突 {summary.conflict}</Tag>
              )}
              {summary.recycleBin > 0 && (
                <Tag color="orange">在回收站 {summary.recycleBin}</Tag>
              )}
              {summary.failed > 0 && (
                <Tag color="red">失败 {summary.failed}</Tag>
              )}
            </div>
            <div
              className="overflow-auto rounded-lg border border-slate-200"
              style={{ maxHeight: 320 }}
            >
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-slate-50 text-left text-xs text-slate-500">
                  <tr>
                    <th className="px-3 py-2">路径</th>
                    <th className="px-3 py-2">结果</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((item, index) => {
                    const label = STATUS_LABELS[item.status];
                    return (
                      <tr key={`${item.relative_path}-${index}`} className="border-t border-slate-100">
                        <td className="px-3 py-2">
                          <div className="break-all">{item.relative_path}</div>
                          {item.error && (
                            <div className="text-xs text-red-500">
                              {item.error}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <Tag color={label.color}>{label.text}</Tag>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="mt-5 flex justify-end gap-3">
              {failedChunks.length > 0 && (
                <Button onClick={retryFailed}>重试失败项</Button>
              )}
              {summary.recycleBin > 0 && (
                <Button
                  onClick={() => {
                    onClose();
                    onOpenRecycleBin();
                  }}
                >
                  前往回收站
                </Button>
              )}
              <Button type="primary" onClick={onClose}>完成</Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
};
