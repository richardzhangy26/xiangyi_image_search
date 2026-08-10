import type { UploadFile } from 'antd/es/upload/interface';

/**
 * 把编辑弹窗的图片列表转换为后端排序负载：
 * 既有资产使用 asset_id（即 UploadFile.uid），新上传文件使用 new:<index> 占位，
 * index 对应 imageFiles 数组下标（与 multipart images 字段顺序一致）。
 */
export function buildImageOrderPayload(fileList: UploadFile[]): {
  imageFiles: File[];
  imageOrder: string[];
} {
  const imageFiles: File[] = [];
  const imageOrder: string[] = [];
  fileList.forEach((file) => {
    if (file.originFileObj) {
      imageOrder.push(`new:${imageFiles.length}`);
      imageFiles.push(file.originFileObj as File);
    } else {
      imageOrder.push(file.uid);
    }
  });
  return { imageFiles, imageOrder };
}

/** 拖拽结束后按 uid 重排，返回新数组；找不到或位置不变时原样返回。 */
export function moveByUid(
  list: UploadFile[],
  activeUid: string,
  overUid: string
): UploadFile[] {
  const from = list.findIndex((item) => item.uid === activeUid);
  const to = list.findIndex((item) => item.uid === overUid);
  if (from < 0 || to < 0 || from === to) {
    return list;
  }
  const next = [...list];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
