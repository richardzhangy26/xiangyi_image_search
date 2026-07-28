import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App.tsx';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: {
            colorPrimary: '#0d7a72',
            colorInfo: '#0d7a72',
            colorLink: '#0d7a72',
            colorTextBase: '#273240',
            colorBgLayout: '#f6f4ef',
            borderRadius: 10,
            fontFamily:
              '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", -apple-system, "Segoe UI", sans-serif',
          },
          components: {
            Button: { controlHeight: 36, fontWeight: 500, paddingInline: 14 },
            Table: {
              headerBg: '#faf9f5',
              headerColor: '#6b7280',
              headerSplitColor: 'transparent',
              rowHoverBg: '#f0f9f8',
              cellPaddingBlock: 14,
            },
            Modal: { borderRadiusLG: 16, titleFontSize: 17 },
            Tag: { borderRadiusSM: 6 },
          },
        }}
      >
        <App />
      </ConfigProvider>
    </BrowserRouter>
  </StrictMode>
);
