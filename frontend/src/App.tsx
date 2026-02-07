/**
 * 主应用组件 - 电子产品配件管理系统
 */
import React, { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ProductUpload } from './components/ProductUpload';
import { ProductSearch } from './components/ProductSearch';
import { Search, Upload } from 'lucide-react';

function App() {
  const [activeTab, setActiveTab] = useState<'search' | 'upload'>('search');

  return (
    <div className="min-h-screen bg-slate-50">
      {/* 现代化导航栏 */}
      <header className="sticky top-0 z-50 bg-white/80 backdrop-blur-md border-b border-gray-200 shadow-sm">
        <div className="max-w-7xl mx-auto px-6 py-4">
          <div className="flex items-center justify-between">
            {/* Logo 和标题 */}
            <div className="flex items-center space-x-4">
              <img
                src="/cropped-LOGO.webp"
                alt="Xiangyi Logo"
                className="h-12 w-auto object-contain"
              />
              <div>
                <h1 className="text-2xl font-bold text-slate-900">
                  电子产品配件管理系统
                </h1>
                <p className="text-sm text-slate-600">
                  以图搜款 · 智能匹配 · 高效管理
                </p>
              </div>
            </div>

            {/* 导航标签 */}
            <nav className="flex space-x-2 bg-slate-100 rounded-lg p-1">
              <button
                onClick={() => setActiveTab('search')}
                className={`
                  flex items-center space-x-2 px-6 py-2.5 rounded-md font-medium
                  transition-all duration-200
                  ${
                    activeTab === 'search'
                      ? 'bg-white text-sky-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900 hover:bg-white/50'
                  }
                `}
              >
                <Search className="w-4 h-4" />
                <span>以图搜款</span>
              </button>
              <button
                onClick={() => setActiveTab('upload')}
                className={`
                  flex items-center space-x-2 px-6 py-2.5 rounded-md font-medium
                  transition-all duration-200
                  ${
                    activeTab === 'upload'
                      ? 'bg-white text-sky-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900 hover:bg-white/50'
                  }
                `}
              >
                <Upload className="w-4 h-4" />
                <span>产品管理</span>
              </button>
            </nav>
          </div>
        </div>
      </header>

      {/* 主内容区域 */}
      <Routes>
        <Route
          path="/"
          element={
            <main className="max-w-7xl mx-auto px-6 py-8">
              <div className="bg-white rounded-xl shadow-sm border border-gray-200">
                {activeTab === 'search' && <ProductSearch />}
                {activeTab === 'upload' && <ProductUpload />}
              </div>
            </main>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

export default App;
