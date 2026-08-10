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
  Tooltip,
  Space,
  Empty,
  Segmented,
  Select,
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  UploadOutlined,
  ReloadOutlined,
  DownloadOutlined,
  ThunderboltOutlined,
  InboxOutlined,
  FileTextOutlined,
  FileImageOutlined,
} from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import type { DragEndEvent } from '@dnd-kit/core';
import { SortableContext, rectSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import type {
  ImageAssetAssignmentResponse,
  ImageAssetManagementItem,
  Product,
  ProductFormData,
  VectorIndexEvent,
} from '../types/product';
import {
  assignImageAssets,
  getProducts,
  createProduct,
  updateProduct,
  deleteProductImage,
  deleteProduct,
  batchDeleteProducts,
  importProductsFromCSV,
  downloadCSVTemplate,
  buildVectorIndex,
  getImageUrl,
  getImageAssets,
} from '../services/productApi';
import { UnassignedAssetGrid } from './UnassignedAssetGrid';
import { buildImageOrderPayload, moveByUid } from './productImageOrder';
import { ImportImagesModal } from './ImportImagesModal';

const ASSET_PAGE_SIZE = 24;
type ManagementView = 'assets' | 'products';

interface SortableUploadItemProps {
  file: UploadFile;
  isPrimary: boolean;
  children: React.ReactNode;
}

/** 可拖拽的图片卡片：首张标记为主图，拖拽改变图片顺序。 */
const SortableUploadItem: React.FC<SortableUploadItemProps> = ({
  file,
  isPrimary,
  children,
}) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: file.uid });

  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Translate.toString(transform),
        transition,
        position: 'relative',
        zIndex: isDragging ? 20 : undefined,
        cursor: 'grab',
      }}
      {...attributes}
      {...listeners}
    >
      {isPrimary && (
        <span className="absolute top-1 left-1 z-10 px-1.5 py-0.5 rounded text-xs font-medium bg-teal-600/90 text-white shadow-sm pointer-events-none">
          主图
        </span>
      )}
      {children}
    </div>
  );
};

/** CSV 必填字段说明（用于导入弹窗展示） */
const CSV_REQUIRED_FIELDS = [
  { key: 'model_number', label: '型号' },
  { key: 'photographer_file', label: '摄影师文件' },
  { key: 'alibaba_product_url', label: '阿里产品链接' },
  { key: 'category', label: '分类' },
];

export const ProductUpload: React.FC = () => {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(false);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [editingProduct, setEditingProduct] = useState<Product | null>(null);
  const [form] = Form.useForm();
  const imageSensors = useSensors(
    // 8px 激活阈值：避免点击预览/删除按钮被误判为拖拽
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } })
  );
  const watchedImages: UploadFile[] = Form.useWatch('images', form) || [];

  const handleImageDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const current =
      (form.getFieldValue('images') as UploadFile[] | undefined) || [];
    form.setFieldsValue({
      images: moveByUid(current, String(active.id), String(over.id)),
    });
  };
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchDeleteLoading, setBatchDeleteLoading] = useState(false);
  const [activeView, setActiveView] = useState<ManagementView>('assets');
  const [assets, setAssets] = useState<ImageAssetManagementItem[]>([]);
  const [assetTotal, setAssetTotal] = useState(0);
  const [assetPage, setAssetPage] = useState(1);
  const [assetSearch, setAssetSearch] = useState('');
  const [assetLoading, setAssetLoading] = useState(false);
  const [assetError, setAssetError] = useState<string | null>(null);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [assignModalOpen, setAssignModalOpen] = useState(false);
  const [createAssignModalOpen, setCreateAssignModalOpen] = useState(false);
  const [targetModelNumber, setTargetModelNumber] = useState<string>();
  const [newModelNumber, setNewModelNumber] = useState('');
  const [assigning, setAssigning] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);

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
    fetchAssets(1, '');
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

  const fetchAssets = async (
    page: number = assetPage,
    search: string = assetSearch
  ) => {
    setAssetLoading(true);
    setAssetError(null);
    try {
      const result = await getImageAssets({
        page,
        perPage: ASSET_PAGE_SIZE,
        search,
      });
      setAssets(result.assets);
      setAssetTotal(result.total);
    } catch (error) {
      setAssetError(
        error instanceof Error ? error.message : '获取待归款图片失败'
      );
    } finally {
      setAssetLoading(false);
    }
  };

  const finishAssignment = async (
    result: ImageAssetAssignmentResponse,
    modelNumber: string
  ) => {
    const total = result.assigned_count + result.reused_count;
    message.success(
      result.product_created
        ? `已创建产品 ${modelNumber} 并关联 ${total} 张图片`
        : `已将 ${total} 张图片关联到 ${modelNumber}`
    );
    setAssignModalOpen(false);
    setCreateAssignModalOpen(false);
    setTargetModelNumber(undefined);
    setNewModelNumber('');
    setSelectedAssetIds([]);
    await Promise.all([
      fetchAssets(assetPage, assetSearch),
      fetchProducts(),
    ]);
  };

  const handleAssignAssets = async () => {
    if (!targetModelNumber || selectedAssetIds.length === 0) return;
    setAssigning(true);
    try {
      const result = await assignImageAssets(
        selectedAssetIds,
        targetModelNumber
      );
      await finishAssignment(result, targetModelNumber);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '关联型号失败');
    } finally {
      setAssigning(false);
    }
  };

  const handleCreateAndAssign = async () => {
    const modelNumber = newModelNumber.trim();
    if (!modelNumber) return;
    if (modelNumber.length > 100) {
      message.error('型号长度不能超过 100 个字符');
      return;
    }
    setAssigning(true);
    try {
      if (selectedAssetIds.length > 0) {
        const result = await assignImageAssets(selectedAssetIds, modelNumber, {
          createIfMissing: true,
        });
        await finishAssignment(result, modelNumber);
      } else {
        await createProduct({ model_number: modelNumber }, []);
        message.success(`已创建产品 ${modelNumber}`);
        setCreateAssignModalOpen(false);
        setNewModelNumber('');
        await fetchProducts();
      }
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '新建产品并关联失败'
      );
    } finally {
      setAssigning(false);
    }
  };

  const refreshAll = () => {
    void Promise.all([fetchProducts(), fetchAssets(assetPage, assetSearch)]);
  };

  const handleAddEdit = () => {
    form.validateFields().then(async (values) => {
      setLoading(true);
      try {
        // 处理图片文件与排序负载（既有资产用 asset_id，新文件用 new:<index> 占位）
        const fileList = (values.images as UploadFile[] | undefined) || [];
        const { imageFiles, imageOrder } = buildImageOrderPayload(fileList);

        const { images, ...productData } = values;

        if (editingProduct) {
          const retainedAssetIds = new Set(
            fileList.map((file) => file.uid)
          );
          const removedAssetIds =
            editingProduct.images
              ?.filter((image) => !retainedAssetIds.has(image.asset_id))
              .map((image) => image.asset_id) || [];

          // 先保存字段、图片顺序和新图片；只有保存成功后才归档被移除的既有资产。
          await updateProduct(
            editingProduct.model_number,
            { ...productData, image_order: imageOrder },
            imageFiles
          );
          const archiveResults = await Promise.allSettled(
            removedAssetIds.map((assetId) =>
              deleteProductImage(editingProduct.model_number, assetId)
            )
          );
          const failedArchiveCount = archiveResults.filter(
            (result) => result.status === 'rejected'
          ).length;
          if (failedArchiveCount > 0) {
            await fetchProducts();
            throw new Error(
              `产品信息已更新，但有 ${failedArchiveCount} 张图片归档失败；请重试保存`
            );
          }
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
        product.images?.map((img) => ({
          uid: img.asset_id,
          name: img.source_relative_path.split('/').pop() || '商品图片',
          status: 'done',
          url: getImageUrl(img.preview_url),
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
      width: 130,
      fixed: 'left' as const,
      render: (text: string) => (
        <span className="font-semibold text-slate-800 tracking-wide">{text}</span>
      ),
    },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 110,
      render: (text: string) =>
        text ? <Tag color="cyan" bordered={false}>{text}</Tag> : '--',
    },
    {
      title: '摄影师文件',
      dataIndex: 'photographer_file',
      key: 'photographer_file',
      width: 130,
      render: (text: string) => <span className="text-slate-500">{text}</span>,
    },
    {
      title: '主图',
      dataIndex: 'images',
      key: 'images',
      width: 100,
      render: (images: any) => {
        const primaryImage =
          images && images.length > 0
            ? images.find((img: any) => img.is_primary) || images[0]
            : null;

        if (!primaryImage) {
          return (
            <div className="w-[60px] h-[60px] rounded-lg bg-slate-100 border border-dashed border-slate-300 flex items-center justify-center">
              <FileImageOutlined className="text-slate-400" />
            </div>
          );
        }

        return (
          <Image
            src={getImageUrl(primaryImage.preview_url)}
            alt="主图"
            width={60}
            height={60}
            className="rounded-lg"
            style={{ objectFit: 'cover', borderRadius: 8 }}
          />
        );
      },
    },
    {
      title: '1688价格',
      dataIndex: 'price_1688',
      key: 'price_1688',
      width: 110,
      render: (price?: number) =>
        price ? (
          <span className="font-medium text-amber-700">¥{price.toFixed(2)}</span>
        ) : (
          <span className="text-slate-400">--</span>
        ),
    },
    {
      title: 'FOB报价',
      key: 'fob_prices',
      width: 180,
      render: (_: any, record: Product) => {
        const tiers = [
          { label: '300-1999', value: record.fob_price_tier1 },
          { label: '2000-9999', value: record.fob_price_tier2 },
          { label: '≥10000', value: record.fob_price_tier3 },
        ].filter((t) => t.value);

        if (tiers.length === 0) return <span className="text-slate-400">--</span>;

        return (
          <div className="space-y-0.5">
            {tiers.map((t) => (
              <div key={t.label} className="text-xs flex items-baseline gap-1.5">
                <span className="text-slate-400 w-[64px]">{t.label}</span>
                <span className="font-medium text-teal-700">${t.value!.toFixed(2)}</span>
              </div>
            ))}
          </div>
        );
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: 'right' as const,
      render: (_: any, record: Product) => (
        <span className="space-x-1">
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => showModal(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确定要删除这个产品吗？"
            onConfirm={() => handleDelete(record.model_number)}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />}>
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
    <div className="p-6 lg:p-8">
      {/* 页头：标题 + 工具栏 */}
      <div className="flex flex-wrap items-end justify-between gap-4 mb-6 animate-rise">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold text-slate-800 tracking-tight m-0">产品管理</h2>
            <Tag bordered={false} className="bg-amber-50 text-amber-700 font-medium">
              {assetTotal.toLocaleString('zh-CN')} 张待归款图片
            </Tag>
            <Tag bordered={false} className="bg-teal-50 text-teal-700 font-medium">
              {products.length.toLocaleString('zh-CN')} 个产品
            </Tag>
          </div>
          <p className="text-sm text-slate-400 mt-1 mb-0">
            维护产品资料与图片，支持 CSV 批量导入与向量索引构建
          </p>
        </div>

        {/* 工具栏：主操作 / 数据操作 / 辅助操作 三组 */}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="primary"
            icon={<PlusOutlined />}
            className="toolbar-btn shadow-sm"
            onClick={() => showModal()}
          >
            添加产品
          </Button>

          <Space.Compact className="toolbar-btn">
            <Button icon={<UploadOutlined />} onClick={() => setCsvModalVisible(true)}>
              CSV 导入
            </Button>
            <Tooltip title="下载 CSV 模板">
              <Button icon={<DownloadOutlined />} onClick={downloadCSVTemplate} />
            </Tooltip>
          </Space.Compact>

          <Button
            icon={<ThunderboltOutlined />}
            className="toolbar-btn"
            style={{ color: '#d97b29', borderColor: '#f0c894', background: '#fdf6ec' }}
            onClick={handleBuildVectorIndex}
            loading={indexingLoading}
          >
            构建向量索引
          </Button>

          <Tooltip title="刷新列表">
            <Button
              className="toolbar-btn"
              icon={<ReloadOutlined />}
              onClick={refreshAll}
              loading={loading || assetLoading}
            />
          </Tooltip>
        </div>
      </div>

      <div className="mb-5 animate-rise-delay-1">
        <Segmented
          value={activeView}
          onChange={(value) => {
            setActiveView(value as ManagementView);
            setSelectedAssetIds([]);
            setSelectedRowKeys([]);
          }}
          options={[
            {
              label: `待归款图片 (${assetTotal.toLocaleString('zh-CN')})`,
              value: 'assets',
            },
            {
              label: `产品资料 (${products.length.toLocaleString('zh-CN')})`,
              value: 'products',
            },
          ]}
        />
      </div>

      {/* 待归款视图导入入口：单图/文件夹/剪贴板导入，不录入产品资料 */}
      {activeView === 'assets' && (
        <div className="animate-rise mb-4 flex items-center justify-between">
          <span className="text-xs text-slate-400">
            导入的图片进入待归款列表并可参与以图搜款，不会录入产品资料
          </span>
          <Button
            icon={<UploadOutlined />}
            onClick={() => setImportModalOpen(true)}
          >
            导入图片
          </Button>
        </div>
      )}

      {/* 批量选择上下文操作条：仅选中时浮现 */}
      {activeView === 'products' && selectedRowKeys.length > 0 && (
        <div className="animate-rise flex items-center justify-between mb-4 px-4 py-2.5 rounded-xl bg-teal-50/80 border border-teal-100">
          <span className="text-sm text-teal-800">
            已选中 <b>{selectedRowKeys.length}</b> 个产品
          </span>
          <Space>
            <Button size="small" onClick={() => setSelectedRowKeys([])}>
              取消选择
            </Button>
            <Popconfirm
              title={`确定要删除选中的 ${selectedRowKeys.length} 个产品吗？`}
              onConfirm={handleBatchDelete}
            >
              <Button
                size="small"
                danger
                type="primary"
                icon={<DeleteOutlined />}
                loading={batchDeleteLoading}
              >
                批量删除
              </Button>
            </Popconfirm>
          </Space>
        </div>
      )}

      {/* 向量索引构建进度 */}
      {activeView === 'products' && showProgress && (
        <div className="animate-rise mb-5 px-5 py-4 rounded-xl bg-white border border-slate-200 shadow-sm">
          <div className="flex items-center gap-2 mb-2">
            <ThunderboltOutlined className="text-amber-600" />
            <span className="text-sm font-medium text-slate-700">向量索引构建</span>
          </div>
          <Progress
            percent={Math.round(progressPercent)}
            strokeColor={{ '0%': '#0d7a72', '100%': '#d97b29' }}
          />
          <p className="mt-1 mb-0 text-xs text-slate-400">{progressMessage}</p>
        </div>
      )}

      <div className="animate-rise-delay-1">
        {activeView === 'assets' ? (
          <UnassignedAssetGrid
            assets={assets}
            total={assetTotal}
            page={assetPage}
            pageSize={ASSET_PAGE_SIZE}
            loading={assetLoading}
            error={assetError}
            search={assetSearch}
            selectedAssetIds={selectedAssetIds}
            hasProducts={products.length > 0}
            onSearch={(value) => {
              setAssetSearch(value);
              setAssetPage(1);
              setSelectedAssetIds([]);
              void fetchAssets(1, value);
            }}
            onPageChange={(page) => {
              setAssetPage(page);
              setSelectedAssetIds([]);
              void fetchAssets(page, assetSearch);
            }}
            onSelectionChange={setSelectedAssetIds}
            onAssign={() => setAssignModalOpen(true)}
            onCreateProduct={() => setCreateAssignModalOpen(true)}
            onRetry={() => void fetchAssets(assetPage, assetSearch)}
          />
        ) : (
          <Table
          columns={columns}
          rowKey="model_number"
          dataSource={products}
          rowSelection={rowSelection}
          loading={loading}
          scroll={{ x: 1200 }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span className="text-slate-400">
                    暂无产品数据，点击「添加产品」或「CSV 导入」开始
                  </span>
                }
              />
            ),
          }}
          pagination={{
            pageSize: 20,
            showQuickJumper: true,
            showTotal: (total) => `共 ${total} 条`,
          }}
          />
        )}
      </div>

      <Modal
        title={`关联 ${selectedAssetIds.length} 张图片`}
        open={assignModalOpen}
        okText="确定关联"
        cancelText="取消"
        confirmLoading={assigning}
        okButtonProps={{ disabled: !targetModelNumber }}
        onOk={handleAssignAssets}
        onCancel={() => {
          setAssignModalOpen(false);
          setTargetModelNumber(undefined);
        }}
      >
        <p className="text-sm text-slate-500 mt-0 mb-3">
          仅可选择已存在的真实产品型号，本批图片将一次性完成关联。
        </p>
        <Select
          showSearch
          value={targetModelNumber}
          placeholder="选择真实产品型号"
          optionFilterProp="label"
          options={products.map((product) => ({
            value: product.model_number,
            label: product.model_number,
          }))}
          onChange={setTargetModelNumber}
          className="w-full"
        />
      </Modal>

      <Modal
        title={selectedAssetIds.length > 0
          ? `新建产品并关联 ${selectedAssetIds.length} 张图片`
          : '添加产品'}
        open={createAssignModalOpen}
        okText={selectedAssetIds.length > 0 ? '创建并关联' : '创建'}
        cancelText="取消"
        confirmLoading={assigning}
        okButtonProps={{ disabled: !newModelNumber.trim() }}
        onOk={handleCreateAndAssign}
        onCancel={() => {
          setCreateAssignModalOpen(false);
          setNewModelNumber('');
        }}
      >
        <p className="text-sm text-slate-500 mt-0 mb-3">
          {selectedAssetIds.length > 0
            ? '只需填写新型号，系统会创建产品并一次性关联本批图片；其余资料可稍后在产品视图中补全。'
            : '只需填写新型号即可创建产品；其余资料可稍后在产品视图中补全，图片也可稍后关联。'}
        </p>
        <Input
          value={newModelNumber}
          placeholder="输入新产品型号"
          maxLength={100}
          aria-label="新产品型号"
          onChange={(event) => setNewModelNumber(event.target.value)}
        />
      </Modal>

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
        styles={{ body: { maxHeight: '70vh', overflow: 'auto' } }}
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
            >
              <Input placeholder="如: photographer_001" />
            </Form.Item>

            <Form.Item
              name="alibaba_product_url"
              label="阿里产品链接"
            >
              <Input placeholder="https://detail.1688.com/offer/..." />
            </Form.Item>

            <Form.Item
              name="category"
              label="分类"
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
            extra="拖拽调整顺序，第一张为主图"
            valuePropName="fileList"
            getValueFromEvent={(e) => {
              if (Array.isArray(e)) {
                return e;
              }
              return e?.fileList;
            }}
          >
            <DndContext
              sensors={imageSensors}
              collisionDetection={closestCenter}
              onDragEnd={handleImageDragEnd}
            >
              <SortableContext
                items={watchedImages.map((file) => file.uid)}
                strategy={rectSortingStrategy}
              >
                <Upload
                  listType="picture-card"
                  multiple
                  beforeUpload={() => false}
                  accept=".png,.jpg,.jpeg,.gif,.webp"
                  itemRender={(originNode, file, fileList) => (
                    <SortableUploadItem
                      file={file}
                      isPrimary={fileList[0]?.uid === file.uid}
                    >
                      {originNode}
                    </SortableUploadItem>
                  )}
                >
                  <div>
                    <PlusOutlined />
                    <div style={{ marginTop: 8 }}>上传图片</div>
                  </div>
                </Upload>
              </SortableContext>
            </DndContext>
          </Form.Item>
        </Form>
      </Modal>

      {/* CSV 导入弹窗 */}
      <Modal
        title={
          <span className="flex items-center gap-2">
            <FileTextOutlined className="text-teal-600" />
            CSV 批量导入产品
          </span>
        }
        open={csvModalVisible}
        onOk={handleCSVImport}
        onCancel={() => {
          setCsvModalVisible(false);
          setCsvFile(null);
        }}
        confirmLoading={csvUploading}
        okText="开始导入"
        cancelText="取消"
        okButtonProps={{ disabled: !csvFile }}
        width={560}
      >
        {/* 拖拽上传区 */}
        <Upload.Dragger
          className="csv-dragger"
          accept=".csv"
          maxCount={1}
          beforeUpload={(file) => {
            setCsvFile(file);
            return false; // 阻止自动上传
          }}
          onRemove={() => setCsvFile(null)}
          fileList={
            csvFile
              ? [{ uid: '-1', name: csvFile.name, size: csvFile.size, status: 'done' as const }]
              : []
          }
        >
          <p className="ant-upload-drag-icon" style={{ marginBottom: 8 }}>
            <InboxOutlined style={{ color: '#0d7a72', fontSize: 42 }} />
          </p>
          <p className="text-slate-700 font-medium mb-1">点击或拖拽 CSV 文件到此处</p>
          <p className="text-xs text-slate-400 m-0">支持 UTF-8 / GBK 编码，单次导入一个文件</p>
        </Upload.Dragger>

        {/* 必填字段说明 */}
        <div className="mt-5 px-4 py-3.5 rounded-xl bg-slate-50 border border-slate-100">
          <div className="text-xs font-medium text-slate-500 mb-2">CSV 必填字段</div>
          <div className="flex flex-wrap gap-1.5">
            {CSV_REQUIRED_FIELDS.map((f) => (
              <Tag key={f.key} bordered={false} className="bg-white text-slate-600 border border-slate-200 m-0">
                <code className="text-teal-700">{f.key}</code>
                <span className="text-slate-400 ml-1">{f.label}</span>
              </Tag>
            ))}
          </div>
          <div className="mt-3 pt-3 border-t border-slate-200/70 flex items-center justify-between">
            <span className="text-xs text-slate-400">不确定格式？先下载模板填写</span>
            <Button
              type="link"
              size="small"
              icon={<DownloadOutlined />}
              onClick={downloadCSVTemplate}
              style={{ padding: 0 }}
            >
              下载 CSV 模板
            </Button>
          </div>
        </div>
      </Modal>

      <ImportImagesModal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        onFinished={() => {
          setAssetPage(1);
          setSelectedAssetIds([]);
          void fetchAssets(1, assetSearch);
        }}
      />
    </div>
  );
};
