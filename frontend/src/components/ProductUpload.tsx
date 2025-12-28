/**
 * 产品上传管理组件 - 电子产品配件
 */
import React, { useState, useEffect } from 'react';
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  message,
  Popconfirm,
  Upload,
  Image,
  Progress,
  Tag,
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  UploadOutlined,
  ReloadOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import type { Product, ProductFormData, VectorIndexEvent } from '../types/product';
import {
  getProducts,
  createProduct,
  updateProduct,
  deleteProduct,
  batchDeleteProducts,
  importProductsFromCSV,
  downloadCSVTemplate,
  buildVectorIndex,
  getImageUrl,
} from '../services/productApi';

export const ProductUpload: React.FC = () => {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(false);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [editingProduct, setEditingProduct] = useState<Product | null>(null);
  const [form] = Form.useForm();
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchDeleteLoading, setBatchDeleteLoading] = useState(false);

  // CSV 导入相关
  const [csvModalVisible, setCsvModalVisible] = useState(false);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvUploading, setCsvUploading] = useState(false);

  // 向量索引构建相关
  const [indexingLoading, setIndexingLoading] = useState(false);
  const [progressPercent, setProgressPercent] = useState<number>(0);
  const [progressMessage, setProgressMessage] = useState<string>('');
  const [showProgress, setShowProgress] = useState<boolean>(false);

  useEffect(() => {
    fetchProducts();
  }, []);

  const fetchProducts = async () => {
    setLoading(true);
    try {
      const data = await getProducts({ page: 0 }); // page=0 返回所有
      setProducts(data.products);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '获取产品列表失败');
    } finally {
      setLoading(false);
    }
  };

  const handleAddEdit = () => {
    form.validateFields().then(async (values) => {
      setLoading(true);
      try {
        // 处理图片文件
        const imageFiles: File[] = [];
        values.images?.forEach((file: UploadFile) => {
          if (file.originFileObj) {
            imageFiles.push(file.originFileObj);
          }
        });

        const { images, ...productData } = values;

        if (editingProduct) {
          // 更新产品
          await updateProduct(editingProduct.model_number, productData, imageFiles);
          message.success('产品更新成功');
        } else {
          // 创建产品
          await createProduct(productData as ProductFormData, imageFiles);
          message.success('产品创建成功');
        }

        setIsModalVisible(false);
        form.resetFields();
        fetchProducts();
      } catch (err) {
        message.error(err instanceof Error ? err.message : '操作失败');
      } finally {
        setLoading(false);
      }
    });
  };

  const handleDelete = async (modelNumber: string) => {
    try {
      await deleteProduct(modelNumber);
      message.success('产品删除成功');
      fetchProducts();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '删除失败');
    }
  };

  const showModal = (product?: Product) => {
    setEditingProduct(product || null);
    if (product) {
      // 编辑模式，回填数据
      const imageList: UploadFile[] =
        product.images?.map((img, idx) => ({
          uid: `image_${idx}`,
          name: img.image_path.split('/').pop() || '',
          status: 'done',
          url: getImageUrl(img.image_path),
        })) || [];

      form.setFieldsValue({
        ...product,
        images: imageList,
      });
    } else {
      // 添加模式
      form.resetFields();
      form.setFieldsValue({ images: [] });
    }
    setIsModalVisible(true);
  };

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请至少选择一项进行删除');
      return;
    }

    setBatchDeleteLoading(true);
    try {
      const modelNumbers = selectedRowKeys as string[];
      const result = await batchDeleteProducts(modelNumbers);
      message.success(result.message || '批量删除成功');
      fetchProducts();
      setSelectedRowKeys([]);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '批量删除失败');
    } finally {
      setBatchDeleteLoading(false);
    }
  };

  // CSV 导入
  const handleCSVImport = async () => {
    if (!csvFile) {
      message.error('请选择 CSV 文件');
      return;
    }

    setCsvUploading(true);
    try {
      const result = await importProductsFromCSV(csvFile);
      message.success(result.message);

      // 显示详细统计
      if (result.stats.errors.length > 0) {
        Modal.info({
          title: 'CSV 导入完成',
          width: 600,
          content: (
            <div>
              <p>成功: {result.stats.success} 条</p>
              <p>失败: {result.stats.failed} 条</p>
              <p>跳过: {result.stats.skipped} 条</p>
              {result.stats.errors.length > 0 && (
                <>
                  <p style={{ marginTop: 12 }}>错误详情:</p>
                  <ul style={{ maxHeight: 300, overflow: 'auto' }}>
                    {result.stats.errors.map((err, idx) => (
                      <li key={idx} style={{ color: 'red' }}>
                        {err}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          ),
        });
      }

      setCsvModalVisible(false);
      setCsvFile(null);
      fetchProducts();
    } catch (err) {
      message.error(err instanceof Error ? err.message : 'CSV 导入失败');
    } finally {
      setCsvUploading(false);
    }
  };

  // 向量索引构建
  const handleBuildVectorIndex = () => {
    setIndexingLoading(true);
    setShowProgress(true);
    setProgressMessage('开始构建向量索引...');
    setProgressPercent(0);

    const cleanup = buildVectorIndex(
      (event: VectorIndexEvent) => {
        switch (event.type) {
          case 'total':
            setProgressMessage(`共有 ${event.value} 个产品需要处理`);
            break;
          case 'progress':
            const percent = event.total > 0 ? (event.processed / event.total) * 100 : 0;
            setProgressPercent(percent);
            setProgressMessage(
              `正在处理: ${event.processed}/${event.total} (型号: ${event.model_number}, 状态: ${event.status})`
            );
            break;
          case 'complete':
            setProgressMessage(event.message);
            setProgressPercent(100);
            message.success(event.message);
            setIndexingLoading(false);
            break;
          case 'error':
            setProgressMessage(`错误: ${event.message}`);
            message.error(event.message);
            setIndexingLoading(false);
            break;
        }
      },
      (error) => {
        message.error(`索引构建错误: ${error}`);
        setIndexingLoading(false);
        setProgressMessage('连接错误，请重试');
      }
    );

    return cleanup;
  };

  const columns = [
    {
      title: '型号',
      dataIndex: 'model_number',
      key: 'model_number',
      width: 120,
      fixed: 'left' as const,
    },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 100,
    },
    {
      title: '摄影师文件',
      dataIndex: 'photographer_file',
      key: 'photographer_file',
      width: 120,
    },
    {
      title: '主图',
      dataIndex: 'images',
      key: 'images',
      width: 100,
      render: (images: any) => {
        if (!images || images.length === 0) return <span>无图片</span>;

        const primaryImage = images.find((img: any) => img.is_primary) || images[0];
        if (!primaryImage) return <span>无图片</span>;

        return (
          <Image
            src={getImageUrl(primaryImage.image_path)}
            alt="主图"
            width={60}
            height={60}
            style={{ objectFit: 'cover' }}
          />
        );
      },
    },
    {
      title: '1688价格',
      dataIndex: 'price_1688',
      key: 'price_1688',
      width: 100,
      render: (price?: number) => (price ? `¥${price.toFixed(2)}` : '--'),
    },
    {
      title: 'FOB报价',
      key: 'fob_prices',
      width: 180,
      render: (_: any, record: Product) => (
        <div style={{ fontSize: '12px' }}>
          {record.fob_price_tier1 && <div>300-1999: ${record.fob_price_tier1.toFixed(2)}</div>}
          {record.fob_price_tier2 && <div>2000-9999: ${record.fob_price_tier2.toFixed(2)}</div>}
          {record.fob_price_tier3 && <div>≥10000: ${record.fob_price_tier3.toFixed(2)}</div>}
        </div>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: 'right' as const,
      render: (_: any, record: Product) => (
        <span className="space-x-2">
          <Button type="text" icon={<EditOutlined />} onClick={() => showModal(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确定要删除这个产品吗？"
            onConfirm={() => handleDelete(record.model_number)}
          >
            <Button type="text" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </span>
      ),
    },
  ];

  const rowSelection = {
    selectedRowKeys,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedRowKeys(newSelectedRowKeys);
    },
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-2xl font-bold">产品管理</h2>
        <div className="space-x-2">
          <Button type="primary" icon={<PlusOutlined />} onClick={() => showModal()}>
            添加产品
          </Button>
          <Button icon={<UploadOutlined />} onClick={() => setCsvModalVisible(true)}>
            CSV 批量导入
          </Button>
          <Button icon={<DownloadOutlined />} onClick={downloadCSVTemplate}>
            下载 CSV 模板
          </Button>
          <Button
            type="primary"
            onClick={handleBuildVectorIndex}
            loading={indexingLoading}
          >
            构建向量索引
          </Button>
          <Button icon={<ReloadOutlined />} onClick={fetchProducts} loading={loading}>
            刷新
          </Button>
          <Popconfirm
            title={`确定要删除选中的 ${selectedRowKeys.length} 个产品吗？`}
            onConfirm={handleBatchDelete}
            disabled={selectedRowKeys.length === 0}
          >
            <Button
              danger
              disabled={selectedRowKeys.length === 0}
              loading={batchDeleteLoading}
            >
              批量删除 {selectedRowKeys.length > 0 && `(${selectedRowKeys.length})`}
            </Button>
          </Popconfirm>
        </div>
      </div>

      {showProgress && (
        <div className="mb-4">
          <Progress percent={Math.round(progressPercent)} />
          <p className="mt-2 text-sm text-gray-600">{progressMessage}</p>
        </div>
      )}

      <Table
        columns={columns}
        rowKey="model_number"
        dataSource={products}
        rowSelection={rowSelection}
        loading={loading}
        scroll={{ x: 1200 }}
        pagination={{
          pageSize: 20,
          showQuickJumper: true,
          showTotal: (total) => `共 ${total} 条`,
        }}
      />

      {/* 添加/编辑产品弹窗 */}
      <Modal
        title={editingProduct ? '编辑产品' : '添加产品'}
        open={isModalVisible}
        onOk={handleAddEdit}
        onCancel={() => {
          setIsModalVisible(false);
          form.resetFields();
        }}
        confirmLoading={loading}
        width={900}
        bodyStyle={{ maxHeight: '70vh', overflow: 'auto' }}
      >
        <Form form={form} layout="vertical">
          <div className="grid grid-cols-2 gap-4">
            {/* 必填字段 */}
            <Form.Item
              name="model_number"
              label="型号"
              rules={[{ required: true, message: '请输入型号' }]}
            >
              <Input placeholder="如: CS-001" />
            </Form.Item>

            <Form.Item
              name="photographer_file"
              label="摄影师文件"
              rules={[{ required: true, message: '请输入摄影师文件' }]}
            >
              <Input placeholder="如: photographer_001" />
            </Form.Item>

            <Form.Item
              name="alibaba_product_url"
              label="阿里产品链接"
              rules={[{ required: true, message: '请输入阿里产品链接' }]}
            >
              <Input placeholder="https://detail.1688.com/offer/..." />
            </Form.Item>

            <Form.Item
              name="category"
              label="分类"
              rules={[{ required: true, message: '请输入分类' }]}
            >
              <Input placeholder="如: 相机肩带" />
            </Form.Item>

            {/* 可选字段 - 参数 */}
            <Form.Item name="spec_cn_reference" label="参数中文（参考）">
              <Input.TextArea rows={2} />
            </Form.Item>

            <Form.Item name="spec_cn" label="参数中文">
              <Input.TextArea rows={2} />
            </Form.Item>

            <Form.Item name="spec_en" label="参数英文">
              <Input.TextArea rows={2} />
            </Form.Item>

            <Form.Item name="product_size" label="产品尺寸">
              <Input placeholder="如: 120cm x 3.8cm" />
            </Form.Item>

            <Form.Item name="package_size" label="包装尺寸">
              <Input placeholder="如: 15cm x 8cm x 3cm" />
            </Form.Item>

            {/* 价格字段 */}
            <Form.Item name="price_1688" label="1688价格 (¥)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            <Form.Item name="fob_price_tier1" label="FOB报价 300-1999 ($)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            <Form.Item name="fob_price_tier2" label="FOB报价 2000-9999 ($)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            <Form.Item name="fob_price_tier3" label="FOB报价 ≥10000 ($)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            <Form.Item name="intl_platform_price" label="国际站定价 ($)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            <Form.Item name="competitor_price" label="国际站同行定价 ($)">
              <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
            </Form.Item>

            {/* 参考链接 */}
            <Form.Item name="ref_link_1" label="参考链接 1">
              <Input placeholder="https://..." />
            </Form.Item>

            <Form.Item name="ref_link_2" label="参考链接 2">
              <Input placeholder="https://..." />
            </Form.Item>

            <Form.Item name="ref_link_3" label="参考链接 3">
              <Input placeholder="https://..." />
            </Form.Item>

            <Form.Item name="intl_platform_url" label="国际站">
              <Input placeholder="https://..." />
            </Form.Item>

            <Form.Item name="intl_platform_url_1" label="国际站 1">
              <Input placeholder="https://..." />
            </Form.Item>

            <Form.Item name="intl_platform_url_2" label="国际站 2">
              <Input placeholder="https://..." />
            </Form.Item>
          </div>

          {/* 图片上传 */}
          <Form.Item
            name="images"
            label="产品图片"
            valuePropName="fileList"
            getValueFromEvent={(e) => {
              if (Array.isArray(e)) {
                return e;
              }
              return e?.fileList;
            }}
          >
            <Upload
              listType="picture-card"
              multiple
              beforeUpload={() => false}
              accept="image/*"
            >
              <div>
                <PlusOutlined />
                <div style={{ marginTop: 8 }}>上传图片</div>
              </div>
            </Upload>
          </Form.Item>
        </Form>
      </Modal>

      {/* CSV 导入弹窗 */}
      <Modal
        title="CSV 批量导入产品"
        open={csvModalVisible}
        onOk={handleCSVImport}
        onCancel={() => {
          setCsvModalVisible(false);
          setCsvFile(null);
        }}
        confirmLoading={csvUploading}
      >
        <Form layout="vertical">
          <Form.Item label="CSV 文件" required>
            <input
              type="file"
              accept=".csv"
              onChange={(e) => {
                if (e.target.files) {
                  setCsvFile(e.target.files[0]);
                }
              }}
            />
          </Form.Item>
          <div style={{ marginTop: 12, color: '#666', fontSize: '12px' }}>
            <p>CSV 文件必须包含以下必填字段：</p>
            <ul style={{ marginLeft: 20 }}>
              <li>model_number (型号)</li>
              <li>photographer_file (摄影师文件)</li>
              <li>alibaba_product_url (阿里产品链接)</li>
              <li>category (分类)</li>
            </ul>
            <p style={{ marginTop: 8 }}>
              <Button type="link" onClick={downloadCSVTemplate} style={{ padding: 0 }}>
                点击下载 CSV 模板
              </Button>
            </p>
          </div>
        </Form>
      </Modal>
    </div>
  );
};
