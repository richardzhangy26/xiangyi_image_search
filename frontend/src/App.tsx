/**
 * 主应用组件 - 电子产品配件管理系统
 */
import React, { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ProductUpload } from './components/ProductUpload';
import { ProductSearch } from './components/ProductSearch';

function App() {
  const [activeTab, setActiveTab] = useState<'search' | 'upload'>('search');

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white shadow">
        <div className="max-w-full mx-auto py-6 px-4">
          <h1 className="text-3xl font-bold text-gray-900">电子产品配件管理系统</h1>
          <p className="text-gray-600 mt-2">以图搜款 | 产品管理 | 图片相似度搜索</p>
        </div>
      </header>

      <Routes>
        <Route
          path="/"
          element={
            <div className="flex">
              {/* 主内容区域 */}
              <main className="flex-1 py-6 px-6">
                <div className="mb-6">
                  <div className="border-b border-gray-200">
                    <nav className="-mb-px flex">
                      <button
                        onClick={() => setActiveTab('search')}
                        className={`${
                          activeTab === 'search'
                            ? 'border-blue-500 text-blue-600'
                            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                        } w-1/2 py-4 px-1 text-center border-b-2 font-medium text-lg`}
                      >
                        以图搜款
                      </button>
                      <button
                        onClick={() => setActiveTab('upload')}
                        className={`${
                          activeTab === 'upload'
                            ? 'border-blue-500 text-blue-600'
                            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                        } w-1/2 py-4 px-1 text-center border-b-2 font-medium text-lg`}
                      >
                        产品管理
                      </button>
                    </nav>
                  </div>
                </div>

                <div className="bg-white shadow rounded-lg">
                  {activeTab === 'search' && <ProductSearch />}
                  {activeTab === 'upload' && <ProductUpload />}
                </div>
              </main>
            </div>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

export default App;
