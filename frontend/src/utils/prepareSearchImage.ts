const MAX_TRANSFER_BYTES = 15 * 1024 * 1024;
const MAX_TRANSFER_PIXELS = 24_000_000;
const MAX_TRANSFER_EDGE = 4096;
const JPEG_QUALITIES = [0.92, 0.84, 0.76, 0.68, 0.6];

const canvasToBlob = (
  canvas: HTMLCanvasElement,
  quality: number
): Promise<Blob> =>
  new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) {
          resolve(blob);
        } else {
          reject(new Error('浏览器无法编码查询图片'));
        }
      },
      'image/jpeg',
      quality
    );
  });

const outputName = (inputName: string): string => {
  const base = inputName.replace(/\.[^.]+$/, '') || 'search-image';
  return `${base}-prepared.jpg`;
};

/**
 * 为 16 MiB 后端请求限制预处理查询图。
 *
 * 小文件且像素安全时返回原 File；只有文件或像素过大时才缩小，不会放大小图。
 * 后端仍会执行可信的 preview-v1 标准化。
 */
export const prepareSearchImage = async (file: File): Promise<File> => {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch {
    throw new Error('图片格式不支持、文件损坏或无法解码');
  }

  try {
    const pixelCount = bitmap.width * bitmap.height;
    if (
      file.size <= MAX_TRANSFER_BYTES &&
      pixelCount <= MAX_TRANSFER_PIXELS &&
      Math.max(bitmap.width, bitmap.height) <= MAX_TRANSFER_EDGE
    ) {
      return file;
    }

    const initialScale = Math.min(
      1,
      MAX_TRANSFER_EDGE / Math.max(bitmap.width, bitmap.height),
      Math.sqrt(MAX_TRANSFER_PIXELS / pixelCount)
    );
    let width = Math.max(1, Math.round(bitmap.width * initialScale));
    let height = Math.max(1, Math.round(bitmap.height * initialScale));

    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    if (!context) {
      throw new Error('浏览器无法处理查询图片');
    }

    while (true) {
      canvas.width = width;
      canvas.height = height;
      context.fillStyle = '#ffffff';
      context.fillRect(0, 0, width, height);
      context.drawImage(bitmap, 0, 0, width, height);

      for (const quality of JPEG_QUALITIES) {
        const blob = await canvasToBlob(canvas, quality);
        if (blob.size <= MAX_TRANSFER_BYTES) {
          return new File([blob], outputName(file.name), {
            type: 'image/jpeg',
            lastModified: file.lastModified,
          });
        }
      }

      if (width === 1 && height === 1) {
        throw new Error('图片过大，浏览器无法压缩到上传限制以内');
      }
      width = Math.max(1, Math.floor(width * 0.8));
      height = Math.max(1, Math.floor(height * 0.8));
    }
  } finally {
    bitmap.close();
  }
};

export {
  MAX_TRANSFER_BYTES,
  MAX_TRANSFER_EDGE,
  MAX_TRANSFER_PIXELS,
};
