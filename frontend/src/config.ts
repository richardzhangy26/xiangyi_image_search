const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(
  /\/+$/,
  ''
);

// 显式环境配置优先；未配置时继续支持本机和局域网零配置访问。
export const API_BASE_URL =
  configuredApiBaseUrl ||
  (window.location.hostname === 'localhost'
    ? 'http://localhost:5000'
    : `http://${window.location.hostname}:5000`);
