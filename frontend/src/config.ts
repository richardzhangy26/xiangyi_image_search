const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(
  /\/+$/,
  ''
);

// 显式环境配置优先；本机开发直连后端 5000；
// 其余场景（局域网 IP / 内网穿透域名）走同源，由 nginx 代理 /api。
export const API_BASE_URL =
  configuredApiBaseUrl ||
  (window.location.hostname === 'localhost'
    ? 'http://localhost:5000'
    : window.location.origin);
