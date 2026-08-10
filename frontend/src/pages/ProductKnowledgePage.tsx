/**
 * 产品知识库页面（K.3）
 *
 * 全员可见的只读浏览页面：左侧目录树 + 搜索，右侧文档查看器。
 * 管理员可创建草稿、编辑、校验、发布。
 *
 * 个人知识库 Tab：以文件目录树方式管理用户的 Markdown 知识文件。
 */

import {
  DeleteOutlined,
  EditOutlined,
  FileOutlined,
  FolderOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Dropdown,
  Empty,
  Input,
  List,
  type MenuProps,
  Modal,
  Popconfirm,
  Row,
  Select,
  Skeleton,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Tree,
  type TreeDataNode,
  Typography,
  Upload,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { knowledgeAdminApi, knowledgeApi, userKnowledgeApi } from '../api/client';
import { useAuth } from '../auth/useAuth';
import KnowledgeTree from '../components/KnowledgeTree';
import KnowledgeUsageBadge from '../components/KnowledgeUsageBadge';
import KnowledgeViewer from '../components/KnowledgeViewer';
import type {
  KnowledgeDocument,
  KnowledgeDraft,
  KnowledgeSearchResult,
  KnowledgeStatus,
  KnowledgeTreeNode,
  KnowledgeUsageItem,
  UserKnowledgeEntry,
  UserProductListItem,
} from '../types';

const { Paragraph, Text, Title } = Typography;

interface HttpLikeError {
  response?: {
    status?: number;
    data?: {
      detail?: string;
    };
  };
  message?: string;
}

function getErrorMessage(error: unknown, fallback: string) {
  const maybeError = error as HttpLikeError;
  return maybeError.response?.data?.detail || maybeError.message || fallback;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

const DOC_TYPE_LABELS: Record<string, string> = {
  overview: '产品概述',
  'market-brief': '市场简报',
  'sales-brief': '销售简报',
  custom: '自定义',
};

interface EntryFormState {
  product_id: string;
  product_scope: 'global' | 'user';
  doc_type: 'overview' | 'market-brief' | 'sales-brief' | 'custom';
  title: string;
  content: string;
  enabled: boolean;
}

export default function ProductKnowledgePage() {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;

  const [tree, setTree] = useState<KnowledgeTreeNode[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [usageMap, setUsageMap] = useState<KnowledgeUsageItem[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedDoc, setSelectedDoc] = useState<KnowledgeDocument | null>(null);
  const [docLoading, setDocLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 搜索状态
  const [searchKeyword, setSearchKeyword] = useState('');
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[]>([]);
  const [searching, setSearching] = useState(false);

  // 草稿编辑状态
  const [draftModalOpen, setDraftModalOpen] = useState(false);
  const [draftLoading, setDraftLoading] = useState(false);
  const [draftContent, setDraftContent] = useState('');
  const [currentDraft, setCurrentDraft] = useState<KnowledgeDraft | null>(null);
  const [draftSaving, setDraftSaving] = useState(false);

  // Tab 切换
  const [activeTab, setActiveTab] = useState<'global' | 'personal'>('global');

  // 个人知识库状态（产品 + 知识条目）
  const [products, setProducts] = useState<UserProductListItem[]>([]);
  const [productsLoading, setProductsLoading] = useState(false);
  const [entries, setEntries] = useState<UserKnowledgeEntry[]>([]);
  const [entriesLoading, setEntriesLoading] = useState(false);

  // 个人知识库目录树选中状态
  const [selectedPersonalKey, setSelectedPersonalKey] = useState<string | null>(null);
  const [inlineEditing, setInlineEditing] = useState(false);
  const [inlineEditContent, setInlineEditContent] = useState('');

  // 产品注册/编辑弹窗
  const [productModalOpen, setProductModalOpen] = useState(false);
  const [editingProduct, setEditingProduct] = useState<UserProductListItem | null>(null);
  const [productForm, setProductForm] = useState({
    name: '',
    description: '',
    aliases: [] as string[],
    keywords: [] as string[],
  });
  const [productSaving, setProductSaving] = useState(false);

  // 知识条目编辑弹窗
  const [entryModalOpen, setEntryModalOpen] = useState(false);
  const [editingEntry, setEditingEntry] = useState<UserKnowledgeEntry | null>(null);
  const [entryForm, setEntryForm] = useState<EntryFormState>({
    product_id: '',
    product_scope: 'user',
    doc_type: 'overview',
    title: '',
    content: '',
    enabled: true,
  });
  const [entrySaving, setEntrySaving] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [treeData, statusData, usageData] = await Promise.all([
        knowledgeApi.getTree(true, true),
        knowledgeApi.getStatus(),
        knowledgeApi.getUsageMap(),
      ]);
      setTree(treeData.children || []);
      setStatus(statusData);
      setUsageMap(usageData);
    } catch (err) {
      setError(getErrorMessage(err, '加载知识库失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const directScoringPaths = useMemo(() => {
    return new Set<string>();
  }, []);

  const handleSelect = useCallback(async (path: string) => {
    setSelectedPath(path);
    setDocLoading(true);
    setSelectedDoc(null);
    try {
      const doc = await knowledgeApi.getDocument(path);
      setSelectedDoc(doc);
    } catch (err) {
      message.error(getErrorMessage(err, '加载文档失败'));
    } finally {
      setDocLoading(false);
    }
  }, []);

  const handleSearch = useCallback(async (value: string) => {
    const keyword = value.trim();
    setSearchKeyword(keyword);
    if (!keyword) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    try {
      const results = await knowledgeApi.search(keyword);
      setSearchResults(results);
    } catch (err) {
      message.error(getErrorMessage(err, '搜索失败'));
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  const handleSearchResultClick = useCallback(
    (item: KnowledgeSearchResult) => {
      setSearchKeyword('');
      setSearchResults([]);
      void handleSelect(item.relative_path);
    },
    [handleSelect],
  );

  // 创建草稿
  const handleCreateDraft = useCallback(async () => {
    if (!selectedDoc) return;
    if (!selectedDoc.editable) {
      message.warning('该文件不允许编辑');
      return;
    }
    setDraftLoading(true);
    try {
      const draft = await knowledgeAdminApi.createDraft(
        selectedDoc.document_id,
        selectedDoc.content_hash,
      );
      setCurrentDraft(draft);
      setDraftContent(draft.content_md);
      setDraftModalOpen(true);
    } catch (err) {
      message.error(getErrorMessage(err, '创建草稿失败'));
    } finally {
      setDraftLoading(false);
    }
  }, [selectedDoc]);

  // 保存草稿
  const handleSaveDraft = useCallback(async () => {
    if (!currentDraft) return;
    if (!draftContent.trim()) {
      message.warning('草稿内容不能为空');
      return;
    }
    setDraftSaving(true);
    try {
      const updated = await knowledgeAdminApi.updateDraft(currentDraft.draft_id, draftContent);
      setCurrentDraft(updated);
      message.success('草稿已保存');
    } catch (err) {
      message.error(getErrorMessage(err, '保存失败'));
    } finally {
      setDraftSaving(false);
    }
  }, [currentDraft, draftContent]);

  // 校验草稿
  const handleValidateDraft = useCallback(async () => {
    if (!currentDraft) return;
    setDraftSaving(true);
    try {
      const result = await knowledgeAdminApi.validateDraft(currentDraft.draft_id);
      if (result.status === 'passed') {
        message.success(
          `校验通过（${result.loader_file_count} 个文件，${result.loader_relevant_count} 个评分相关）`,
        );
      } else {
        message.warning(`校验失败：${result.errors.join('; ')}`);
      }
      // 刷新草稿状态
      const data = await knowledgeAdminApi.getDraft(currentDraft.draft_id);
      setCurrentDraft(data.draft);
    } catch (err) {
      message.error(getErrorMessage(err, '校验失败'));
    } finally {
      setDraftSaving(false);
    }
  }, [currentDraft]);

  // 发布草稿
  const handlePublishDraft = useCallback(async () => {
    if (!currentDraft) return;
    Modal.confirm({
      title: '发布到正式知识库',
      content: '发布后将立即生效，影响后续文章打分和草稿生成。确认发布？',
      okText: '发布',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDraftSaving(true);
        try {
          await knowledgeAdminApi.publish([currentDraft.draft_id]);
          message.success('发布成功，知识库已刷新');
          setDraftModalOpen(false);
          setCurrentDraft(null);
          await loadData();
        } catch (err) {
          message.error(getErrorMessage(err, '发布失败'));
        } finally {
          setDraftSaving(false);
        }
      },
    });
  }, [currentDraft, loadData]);

  // 放弃草稿
  const handleDiscardDraft = useCallback(async () => {
    if (!currentDraft) return;
    Modal.confirm({
      title: '放弃草稿',
      content: '放弃后草稿将被删除，不影响正式文件。',
      okText: '放弃',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDraftSaving(true);
        try {
          await knowledgeAdminApi.deleteDraft(currentDraft.draft_id);
          message.success('草稿已放弃');
          setDraftModalOpen(false);
          setCurrentDraft(null);
        } catch (err) {
          message.error(getErrorMessage(err, '放弃失败'));
        } finally {
          setDraftSaving(false);
        }
      },
    });
  }, [currentDraft]);

  // ── 个人知识库（产品 + 知识条目）──

  const loadProducts = useCallback(async () => {
    setProductsLoading(true);
    try {
      const items = await userKnowledgeApi.listProducts();
      setProducts(items);
    } catch (err) {
      message.error(getErrorMessage(err, '加载产品列表失败'));
    } finally {
      setProductsLoading(false);
    }
  }, []);

  const loadEntries = useCallback(async () => {
    setEntriesLoading(true);
    try {
      const items = await userKnowledgeApi.listEntries();
      setEntries(items);
    } catch (err) {
      message.error(getErrorMessage(err, '加载知识条目失败'));
    } finally {
      setEntriesLoading(false);
    }
  }, []);

  const loadPersonalData = useCallback(async () => {
    await Promise.all([loadProducts(), loadEntries()]);
  }, [loadProducts, loadEntries]);

  const handleTabChange = useCallback(
    (key: string) => {
      setActiveTab(key as 'global' | 'personal');
      if (key === 'personal') {
        void loadPersonalData();
      }
    },
    [loadPersonalData],
  );

  // 产品名映射，用于条目列表展示
  const productNameMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of products) {
      map.set(p.product_id, p.name);
    }
    return map;
  }, [products]);

  const userProducts = useMemo(() => products.filter((p) => p.scope === 'user'), [products]);
  const globalProducts = useMemo(() => products.filter((p) => p.scope === 'global'), [products]);

  // 当前选中的知识条目（文件节点）
  const selectedEntry = useMemo(() => {
    if (!selectedPersonalKey?.startsWith('entry:')) return null;
    const entryId = selectedPersonalKey.slice('entry:'.length);
    return entries.find((e) => e.entry_id === entryId) ?? null;
  }, [selectedPersonalKey, entries]);

  // 当前选中的产品（文件夹节点）
  const selectedProduct = useMemo(() => {
    if (!selectedPersonalKey?.startsWith('product:')) return null;
    const productId = selectedPersonalKey.slice('product:'.length);
    return products.find((p) => p.product_id === productId) ?? null;
  }, [selectedPersonalKey, products]);

  // 产品注册/编辑
  const openCreateProduct = useCallback(() => {
    setEditingProduct(null);
    setProductForm({ name: '', description: '', aliases: [], keywords: [] });
    setProductModalOpen(true);
  }, []);

  const openEditProduct = useCallback((product: UserProductListItem) => {
    setEditingProduct(product);
    setProductForm({
      name: product.name,
      description: product.description,
      aliases: [...product.aliases],
      keywords: [...product.keywords],
    });
    setProductModalOpen(true);
  }, []);

  const handleSaveProduct = useCallback(async () => {
    if (!productForm.name.trim()) {
      message.warning('请输入产品名称');
      return;
    }
    setProductSaving(true);
    try {
      const body = {
        name: productForm.name.trim(),
        description: productForm.description.trim(),
        aliases: productForm.aliases,
        keywords: productForm.keywords,
      };
      if (editingProduct) {
        await userKnowledgeApi.updateProduct(editingProduct.product_id, body);
        message.success('产品已更新');
      } else {
        await userKnowledgeApi.createProduct(body);
        message.success('产品已注册');
      }
      setProductModalOpen(false);
      await loadProducts();
    } catch (err) {
      message.error(getErrorMessage(err, '保存产品失败'));
    } finally {
      setProductSaving(false);
    }
  }, [productForm, editingProduct, loadProducts]);

  const performDeleteProduct = useCallback(
    async (product: UserProductListItem) => {
      try {
        await userKnowledgeApi.deleteProduct(product.product_id);
        message.success('产品已删除');
        if (selectedPersonalKey === `product:${product.product_id}`) {
          setSelectedPersonalKey(null);
        }
        await loadProducts();
      } catch (err) {
        const maybeError = err as HttpLikeError;
        if (maybeError.response?.status === 409) {
          message.error('该产品仍有关联知识条目，请先删除关联条目');
        } else {
          message.error(getErrorMessage(err, '删除产品失败'));
        }
      }
    },
    [loadProducts, selectedPersonalKey],
  );

  const handleDeleteProduct = useCallback(
    (product: UserProductListItem) => {
      Modal.confirm({
        title: '删除产品',
        content: `确认删除产品「${product.name}」？`,
        okText: '删除',
        cancelText: '取消',
        okButtonProps: { danger: true },
        onOk: () => performDeleteProduct(product),
      });
    },
    [performDeleteProduct],
  );

  // 知识条目编辑
  const openCreateEntry = useCallback(
    (productId?: string) => {
      setEditingEntry(null);
      const product = productId ? products.find((p) => p.product_id === productId) : products[0];
      setEntryForm({
        product_id: product?.product_id ?? '',
        product_scope: product?.scope ?? 'user',
        doc_type: 'overview',
        title: '',
        content: '',
        enabled: true,
      });
      setEntryModalOpen(true);
    },
    [products],
  );

  const openEditEntry = useCallback((entry: UserKnowledgeEntry) => {
    setEditingEntry(entry);
    setEntryForm({
      product_id: entry.product_id,
      product_scope: entry.product_scope,
      doc_type: entry.doc_type,
      title: entry.title,
      content: entry.content,
      enabled: entry.enabled,
    });
    setEntryModalOpen(true);
  }, []);

  const handleEntryProductChange = useCallback(
    (productId: string) => {
      const product = products.find((p) => p.product_id === productId);
      setEntryForm((prev) => ({
        ...prev,
        product_id: productId,
        product_scope: product?.scope ?? 'user',
      }));
    },
    [products],
  );

  const handleEntryFileUpload = useCallback((file: File) => {
    const extension = file.name.includes('.')
      ? `.${file.name.split('.').pop()?.toLowerCase()}`
      : '';
    if (extension !== '.md') {
      message.error('仅支持上传 .md 文件');
      return false;
    }
    if (file.size > 5 * 1024 * 1024) {
      message.error('文件不能超过 5MB');
      return false;
    }
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = (e.target?.result as string) ?? '';
      setEntryForm((prev) => ({
        ...prev,
        content: text,
        title: prev.title.trim() || file.name.replace(/\.md$/i, ''),
      }));
      message.success(`已解析 ${file.name}（${text.length} 字）`);
    };
    reader.onerror = () => message.error('文件读取失败，请重试');
    reader.readAsText(file, 'utf-8');
    return false;
  }, []);

  const handleSaveEntry = useCallback(async () => {
    if (!entryForm.product_id) {
      message.warning('请选择关联产品');
      return;
    }
    if (!entryForm.title.trim()) {
      message.warning('请输入标题');
      return;
    }
    if (!entryForm.content.trim()) {
      message.warning('请输入内容');
      return;
    }
    setEntrySaving(true);
    try {
      const body = {
        product_id: entryForm.product_id,
        product_scope: entryForm.product_scope,
        doc_type: entryForm.doc_type,
        title: entryForm.title.trim(),
        content: entryForm.content,
        enabled: entryForm.enabled,
      };
      if (editingEntry) {
        await userKnowledgeApi.updateEntry(editingEntry.entry_id, body);
        message.success('条目已更新');
      } else {
        await userKnowledgeApi.createEntry(body);
        message.success('条目已创建');
      }
      setEntryModalOpen(false);
      await loadEntries();
    } catch (err) {
      message.error(getErrorMessage(err, '保存条目失败'));
    } finally {
      setEntrySaving(false);
    }
  }, [entryForm, editingEntry, loadEntries]);

  const performDeleteEntry = useCallback(
    async (entry: UserKnowledgeEntry) => {
      try {
        await userKnowledgeApi.deleteEntry(entry.entry_id);
        message.success('条目已删除');
        if (selectedPersonalKey === `entry:${entry.entry_id}`) {
          setSelectedPersonalKey(null);
          setInlineEditing(false);
        }
        await loadEntries();
      } catch (err) {
        message.error(getErrorMessage(err, '删除条目失败'));
      }
    },
    [loadEntries, selectedPersonalKey],
  );

  const handleDeleteEntry = useCallback(
    (entry: UserKnowledgeEntry) => {
      Modal.confirm({
        title: '删除知识条目',
        content: `确认删除条目「${entry.title}」？`,
        okText: '删除',
        cancelText: '取消',
        okButtonProps: { danger: true },
        onOk: () => performDeleteEntry(entry),
      });
    },
    [performDeleteEntry],
  );

  const handleToggleEntry = useCallback(async (entry: UserKnowledgeEntry) => {
    try {
      const result = await userKnowledgeApi.toggleEntry(entry.entry_id);
      setEntries((prev) =>
        prev.map((e) => (e.entry_id === entry.entry_id ? { ...e, enabled: result.enabled } : e)),
      );
    } catch (err) {
      message.error(getErrorMessage(err, '切换状态失败'));
    }
  }, []);

  // 内联编辑（文件内容快速编辑）
  const startInlineEdit = useCallback((entry: UserKnowledgeEntry) => {
    setSelectedPersonalKey(`entry:${entry.entry_id}`);
    setInlineEditContent(entry.content);
    setInlineEditing(true);
  }, []);

  const saveInlineEdit = useCallback(async () => {
    if (!selectedEntry) return;
    if (!inlineEditContent.trim()) {
      message.warning('内容不能为空');
      return;
    }
    setEntrySaving(true);
    try {
      await userKnowledgeApi.updateEntry(selectedEntry.entry_id, {
        content: inlineEditContent,
      });
      message.success('条目已更新');
      setInlineEditing(false);
      await loadEntries();
    } catch (err) {
      message.error(getErrorMessage(err, '保存条目失败'));
    } finally {
      setEntrySaving(false);
    }
  }, [selectedEntry, inlineEditContent, loadEntries]);

  const cancelInlineEdit = useCallback(() => {
    setInlineEditing(false);
    setInlineEditContent('');
  }, []);

  // 构建个人知识库目录树数据（产品=文件夹，条目=文件）
  const personalTreeData = useMemo<TreeDataNode[]>(() => {
    const entriesByProduct = new Map<string, UserKnowledgeEntry[]>();
    for (const entry of entries) {
      const list = entriesByProduct.get(entry.product_id) ?? [];
      list.push(entry);
      entriesByProduct.set(entry.product_id, list);
    }

    // 用户级产品在前，全局产品在后
    const sortedProducts = [...userProducts, ...globalProducts];

    return sortedProducts.map((product) => {
      const productEntries = entriesByProduct.get(product.product_id) ?? [];
      const isGlobal = product.scope === 'global';
      const scopeLabel = isGlobal ? '全局' : '我的';

      const folderMenu: MenuProps = {
        items: [
          { key: 'new-entry', label: '新建文件' },
          { key: 'edit-product', label: '编辑产品信息', disabled: isGlobal },
          { type: 'divider' },
          { key: 'delete-product', label: '删除产品', danger: true, disabled: isGlobal },
        ],
        onClick: ({ key: menuKey }) => {
          if (menuKey === 'new-entry') openCreateEntry(product.product_id);
          else if (menuKey === 'edit-product') openEditProduct(product);
          else if (menuKey === 'delete-product') handleDeleteProduct(product);
        },
      };

      return {
        key: `product:${product.product_id}`,
        title: (
          <Dropdown menu={folderMenu} trigger={['contextMenu']}>
            <span>
              {product.name}
              <span style={{ color: '#999', fontSize: 12, marginLeft: 4 }}>({scopeLabel})</span>
            </span>
          </Dropdown>
        ),
        icon: <FolderOutlined />,
        children: productEntries.map((entry) => {
          const fileMenu: MenuProps = {
            items: [
              { key: 'edit', label: '编辑' },
              { key: 'edit-info', label: '编辑信息' },
              { key: 'toggle', label: entry.enabled ? '禁用' : '启用' },
              { type: 'divider' },
              { key: 'delete', label: '删除', danger: true },
            ],
            onClick: ({ key: menuKey }) => {
              if (menuKey === 'edit') startInlineEdit(entry);
              else if (menuKey === 'edit-info') openEditEntry(entry);
              else if (menuKey === 'toggle') void handleToggleEntry(entry);
              else if (menuKey === 'delete') handleDeleteEntry(entry);
            },
          };

          return {
            key: `entry:${entry.entry_id}`,
            title: (
              <Dropdown menu={fileMenu} trigger={['contextMenu']}>
                <span>
                  {entry.title}
                  <span style={{ color: '#999', fontSize: 12 }}>.md</span>
                </span>
              </Dropdown>
            ),
            icon: <FileOutlined />,
            isLeaf: true,
          };
        }),
      };
    });
  }, [
    userProducts,
    globalProducts,
    entries,
    openCreateEntry,
    openEditProduct,
    handleDeleteProduct,
    startInlineEdit,
    openEditEntry,
    handleToggleEntry,
    handleDeleteEntry,
  ]);

  // 个人知识库右侧内容区
  const renderPersonalRightPanel = () => {
    // 选中文件（知识条目）时
    if (selectedEntry) {
      return (
        <Card title={`${selectedEntry.title}.md`} size="small" styles={{ body: { padding: 16 } }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space wrap>
              <Tag>{productNameMap.get(selectedEntry.product_id) || selectedEntry.product_id}</Tag>
              <Tag color="geekblue">{DOC_TYPE_LABELS[selectedEntry.doc_type]}</Tag>
              <Space>
                <Switch
                  size="small"
                  checked={selectedEntry.enabled}
                  onChange={() => void handleToggleEntry(selectedEntry)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {selectedEntry.enabled ? '已启用' : '已禁用'}
                </Text>
              </Space>
            </Space>

            {inlineEditing ? (
              <>
                <Input.TextArea
                  value={inlineEditContent}
                  onChange={(e) => setInlineEditContent(e.target.value)}
                  rows={20}
                  style={{ fontFamily: 'monospace', fontSize: 13 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {inlineEditContent.length} 字
                </Text>
                <Space>
                  <Button type="primary" onClick={saveInlineEdit} loading={entrySaving}>
                    保存
                  </Button>
                  <Button onClick={cancelInlineEdit}>取消</Button>
                </Space>
              </>
            ) : (
              <>
                <div
                  style={{
                    background: '#fafafa',
                    border: '1px solid #f0f0f0',
                    borderRadius: 4,
                    padding: 16,
                    maxHeight: '50vh',
                    overflow: 'auto',
                  }}
                >
                  <ReactMarkdown>{selectedEntry.content || ''}</ReactMarkdown>
                </div>
                <Space>
                  <Button icon={<EditOutlined />} onClick={() => startInlineEdit(selectedEntry)}>
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除知识条目"
                    description={`确认删除条目「${selectedEntry.title}」？`}
                    onConfirm={() => void performDeleteEntry(selectedEntry)}
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                  >
                    <Button danger icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              </>
            )}
          </Space>
        </Card>
      );
    }

    // 选中文件夹（产品）时
    if (selectedProduct) {
      const isGlobal = selectedProduct.scope === 'global';
      return (
        <Card title={selectedProduct.name} size="small" styles={{ body: { padding: 16 } }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <div>
              <Text type="secondary">名称：</Text>
              <Text strong>{selectedProduct.name}</Text>
              <Tag color={isGlobal ? 'default' : 'blue'} style={{ marginLeft: 8 }}>
                {isGlobal ? '全局' : '用户级'}
              </Tag>
            </div>
            {selectedProduct.description && (
              <div>
                <Text type="secondary">描述：</Text>
                <Text>{selectedProduct.description}</Text>
              </div>
            )}
            {selectedProduct.aliases.length > 0 && (
              <div>
                <Text type="secondary">别名：</Text>
                <Space size={4} wrap>
                  {selectedProduct.aliases.map((alias) => (
                    <Tag key={alias}>{alias}</Tag>
                  ))}
                </Space>
              </div>
            )}
            {selectedProduct.keywords.length > 0 && (
              <div>
                <Text type="secondary">关键词：</Text>
                <Space size={4} wrap>
                  {selectedProduct.keywords.map((keyword) => (
                    <Tag key={keyword} color="green">
                      {keyword}
                    </Tag>
                  ))}
                </Space>
              </div>
            )}
            <Divider style={{ margin: '8px 0' }} />
            <Space>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => openCreateEntry(selectedProduct.product_id)}
              >
                新建知识条目
              </Button>
              {!isGlobal && (
                <>
                  <Button icon={<EditOutlined />} onClick={() => openEditProduct(selectedProduct)}>
                    编辑产品
                  </Button>
                  <Popconfirm
                    title="删除产品"
                    description={`确认删除产品「${selectedProduct.name}」？`}
                    onConfirm={() => void performDeleteProduct(selectedProduct)}
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                  >
                    <Button danger icon={<DeleteOutlined />}>
                      删除产品
                    </Button>
                  </Popconfirm>
                </>
              )}
            </Space>
          </Space>
        </Card>
      );
    }

    // 未选中时
    return (
      <Card size="small" styles={{ body: { padding: 16 } }}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="请选择左侧目录树中的文件或文件夹"
          style={{ padding: '40px 0' }}
        />
      </Card>
    );
  };

  const renderLeftContent = () => {
    if (searchKeyword) {
      return (
        <Spin spinning={searching}>
          {searchResults.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="未找到匹配文件"
              style={{ padding: 24 }}
            />
          ) : (
            <List
              size="small"
              dataSource={searchResults}
              renderItem={(item) => (
                <List.Item
                  style={{ cursor: 'pointer', padding: '8px 4px' }}
                  onClick={() => handleSearchResultClick(item)}
                >
                  <Space direction="vertical" size={0} style={{ width: '100%' }}>
                    <Space>
                      <Text strong>{item.name}</Text>
                      <KnowledgeUsageBadge role={item.knowledge_role} />
                      {item.direct_scoring_prompt && (
                        <Tag color="gold" style={{ marginInlineStart: 0 }}>
                          核心打分
                        </Tag>
                      )}
                    </Space>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {item.relative_path}
                    </Text>
                    {item.snippet && (
                      <Text type="secondary" ellipsis style={{ fontSize: 12 }}>
                        {item.snippet}
                      </Text>
                    )}
                  </Space>
                </List.Item>
              )}
            />
          )}
        </Spin>
      );
    }
    return (
      <KnowledgeTree
        treeData={tree}
        onSelect={handleSelect}
        directScoringPaths={directScoringPaths}
        selectedPath={selectedPath || undefined}
      />
    );
  };

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>
            产品知识库
          </Title>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            浏览产品知识库的正式文档和目录结构
          </Paragraph>
        </Col>
        <Col>
          <Space>
            {activeTab === 'global' && isAdmin && selectedDoc?.editable && (
              <Button
                type="primary"
                icon={<EditOutlined />}
                loading={draftLoading}
                onClick={handleCreateDraft}
              >
                编辑
              </Button>
            )}
            <Button
              icon={<ReloadOutlined />}
              loading={activeTab === 'global' ? loading : productsLoading || entriesLoading}
              onClick={activeTab === 'global' ? loadData : loadPersonalData}
            >
              刷新
            </Button>
          </Space>
        </Col>
      </Row>

      <Tabs
        activeKey={activeTab}
        onChange={handleTabChange}
        items={[
          {
            key: 'global',
            label: '全局知识库',
            children: (
              <>
                {error && (
                  <Alert
                    type="error"
                    showIcon
                    closable
                    message="知识库加载异常"
                    description={error}
                    onClose={() => setError(null)}
                    style={{ marginBottom: 16 }}
                  />
                )}

                {/* 状态卡片 */}
                <Card size="small" style={{ marginBottom: 16 }}>
                  {loading ? (
                    <Skeleton active paragraph={{ rows: 1 }} />
                  ) : (
                    <Row gutter={[24, 8]} align="middle">
                      <Col>
                        <Text type="secondary">知识哈希：</Text>
                        <Text code>{status?.knowledge_hash?.slice(0, 8) || '-'}</Text>
                      </Col>
                      <Col>
                        <Text type="secondary">加载时间：</Text>
                        <Text>{status?.loaded_at ? formatTime(status.loaded_at) : '-'}</Text>
                      </Col>
                      <Col>
                        <Text type="secondary">文件总数：</Text>
                        <Text strong>{status?.file_count ?? 0}</Text>
                      </Col>
                      <Col>
                        <Text type="secondary">评分相关：</Text>
                        <Text strong>{status?.loader_relevant_count ?? 0}</Text>
                      </Col>
                      <Col>
                        <Text type="secondary">核心打分：</Text>
                        <Text strong>{status?.direct_scoring_file_count ?? 0}</Text>
                      </Col>
                      <Col>
                        <Tag color={status?.loaded ? 'green' : 'default'}>
                          {status?.loaded ? '已加载' : '未加载'}
                        </Tag>
                      </Col>
                    </Row>
                  )}
                </Card>

                {/* 用途图例 */}
                {usageMap.length > 0 && (
                  <Card size="small" style={{ marginBottom: 16 }}>
                    <Space wrap>
                      {usageMap.map((item) => (
                        <KnowledgeUsageBadge key={item.role} role={item.role} label={item.label} />
                      ))}
                    </Space>
                  </Card>
                )}

                <Row gutter={16}>
                  <Col xs={24} md={8}>
                    <Card title="目录" size="small" styles={{ body: { padding: 12 } }}>
                      <Input.Search
                        placeholder="搜索文件名或内容"
                        prefix={<SearchOutlined />}
                        allowClear
                        enterButton
                        value={searchKeyword}
                        onChange={(e) => {
                          const val = e.target.value;
                          if (!val) {
                            setSearchResults([]);
                          }
                          setSearchKeyword(val);
                        }}
                        onSearch={handleSearch}
                        style={{ marginBottom: 12 }}
                      />
                      {loading ? <Skeleton active paragraph={{ rows: 6 }} /> : renderLeftContent()}
                    </Card>
                  </Col>
                  <Col xs={24} md={16}>
                    <Card title="文档内容" size="small" styles={{ body: { padding: 16 } }}>
                      <KnowledgeViewer document={selectedDoc} loading={docLoading} />
                    </Card>
                  </Col>
                </Row>
              </>
            ),
          },
          {
            key: 'personal',
            label: '个人知识库',
            children: (
              <Row gutter={16}>
                <Col xs={24} md={8}>
                  <Card
                    title="目录树"
                    size="small"
                    styles={{ body: { padding: 12 } }}
                    extra={
                      <Button
                        size="small"
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={openCreateProduct}
                      >
                        新建产品
                      </Button>
                    }
                  >
                    {productsLoading || entriesLoading ? (
                      <Skeleton active paragraph={{ rows: 6 }} />
                    ) : personalTreeData.length === 0 ? (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description="暂无产品"
                        style={{ padding: 24 }}
                      />
                    ) : (
                      <Tree
                        treeData={personalTreeData}
                        showIcon
                        blockNode
                        selectedKeys={selectedPersonalKey ? [selectedPersonalKey] : []}
                        onSelect={(keys) => {
                          if (keys.length > 0 && typeof keys[0] === 'string') {
                            setSelectedPersonalKey(keys[0]);
                            setInlineEditing(false);
                          } else {
                            setSelectedPersonalKey(null);
                          }
                        }}
                        style={{ overflowX: 'auto', maxHeight: '60vh' }}
                      />
                    )}
                  </Card>
                </Col>
                <Col xs={24} md={16}>
                  {renderPersonalRightPanel()}
                </Col>
              </Row>
            ),
          },
        ]}
      />

      {/* 草稿编辑弹窗 */}
      <Modal
        title={`编辑草稿 - ${currentDraft?.relative_path ?? ''}`}
        open={draftModalOpen}
        onCancel={() => {
          setDraftModalOpen(false);
          setCurrentDraft(null);
        }}
        width={900}
        footer={
          <Space>
            <Button danger onClick={handleDiscardDraft} loading={draftSaving}>
              放弃草稿
            </Button>
            <Button onClick={handleValidateDraft} loading={draftSaving}>
              校验
            </Button>
            <Button onClick={handleSaveDraft} loading={draftSaving}>
              保存
            </Button>
            <Button type="primary" onClick={handlePublishDraft} loading={draftSaving}>
              发布
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {currentDraft?.validation && (
            <Alert
              type={currentDraft.validation.status === 'passed' ? 'success' : 'error'}
              showIcon
              message={
                currentDraft.validation.status === 'passed'
                  ? '校验通过'
                  : `校验失败：${currentDraft.validation.errors.join('; ')}`
              }
              description={
                currentDraft.validation.warnings.length > 0
                  ? currentDraft.validation.warnings.join('\n')
                  : undefined
              }
            />
          )}
          <Input.TextArea
            value={draftContent}
            onChange={(e) => setDraftContent(e.target.value)}
            rows={20}
            style={{ fontFamily: 'monospace', fontSize: 13 }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            草稿 ID: {currentDraft?.draft_id ?? '-'} · 状态: {currentDraft?.status ?? '-'} ·
            更新时间: {currentDraft?.updated_at ? formatTime(currentDraft.updated_at) : '-'}
          </Text>
        </Space>
      </Modal>

      {/* 产品注册/编辑弹窗 */}
      <Modal
        title={editingProduct ? '编辑产品' : '注册新产品'}
        open={productModalOpen}
        onCancel={() => setProductModalOpen(false)}
        onOk={handleSaveProduct}
        confirmLoading={productSaving}
        okText="保存"
        cancelText="取消"
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              产品名称
            </Text>
            <Input
              value={productForm.name}
              onChange={(e) => setProductForm((prev) => ({ ...prev, name: e.target.value }))}
              placeholder="产品名称"
            />
          </div>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              产品描述
            </Text>
            <Input.TextArea
              value={productForm.description}
              onChange={(e) => setProductForm((prev) => ({ ...prev, description: e.target.value }))}
              rows={3}
              placeholder="可选"
            />
          </div>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              别名
            </Text>
            <Select
              mode="tags"
              style={{ width: '100%' }}
              value={productForm.aliases}
              onChange={(values) =>
                setProductForm((prev) => ({ ...prev, aliases: values as string[] }))
              }
              placeholder="输入后按回车添加"
            />
          </div>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              关键词
            </Text>
            <Select
              mode="tags"
              style={{ width: '100%' }}
              value={productForm.keywords}
              onChange={(values) =>
                setProductForm((prev) => ({ ...prev, keywords: values as string[] }))
              }
              placeholder="输入后按回车添加"
            />
          </div>
        </Space>
      </Modal>

      {/* 知识条目编辑弹窗 */}
      <Modal
        title={editingEntry ? '编辑知识条目' : '新增知识条目'}
        open={entryModalOpen}
        onCancel={() => setEntryModalOpen(false)}
        onOk={handleSaveEntry}
        confirmLoading={entrySaving}
        okText="保存"
        cancelText="取消"
        width={800}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              关联产品
            </Text>
            <Select
              style={{ width: '100%' }}
              value={entryForm.product_id || undefined}
              onChange={handleEntryProductChange}
              placeholder="选择产品"
            >
              {products.map((p) => (
                <Select.Option key={p.product_id} value={p.product_id}>
                  {p.name}（{p.scope === 'global' ? '全局' : '用户级'}）
                </Select.Option>
              ))}
            </Select>
          </div>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              文档类型
            </Text>
            <Select
              style={{ width: '100%' }}
              value={entryForm.doc_type}
              onChange={(value) =>
                setEntryForm((prev) => ({
                  ...prev,
                  doc_type: value as EntryFormState['doc_type'],
                }))
              }
            >
              <Select.Option value="overview">产品概述</Select.Option>
              <Select.Option value="market-brief">市场简报</Select.Option>
              <Select.Option value="sales-brief">销售简报</Select.Option>
              <Select.Option value="custom">自定义</Select.Option>
            </Select>
          </div>
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              标题
            </Text>
            <Input
              value={entryForm.title}
              onChange={(e) => setEntryForm((prev) => ({ ...prev, title: e.target.value }))}
              placeholder="条目标题"
            />
          </div>
          {!editingEntry && (
            <div>
              <Text strong style={{ display: 'block', marginBottom: 4 }}>
                上传 Markdown 文件（可选）
              </Text>
              <Upload.Dragger
                accept=".md"
                maxCount={1}
                showUploadList={false}
                beforeUpload={handleEntryFileUpload}
              >
                <p className="ant-upload-drag-icon">
                  <InboxOutlined />
                </p>
                <p className="ant-upload-text">点击或拖拽 .md 文件到此区域</p>
                <p className="ant-upload-hint">上传后自动解析文本填充到内容和标题</p>
              </Upload.Dragger>
            </div>
          )}
          <div>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              内容（Markdown）
            </Text>
            <Input.TextArea
              value={entryForm.content}
              onChange={(e) => setEntryForm((prev) => ({ ...prev, content: e.target.value }))}
              rows={20}
              style={{ fontFamily: 'monospace', fontSize: 13 }}
              placeholder="Markdown 内容..."
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {entryForm.content.length} 字 · 预估 {Math.ceil(entryForm.content.length / 2)} Token
            </Text>
          </div>
        </Space>
      </Modal>
    </div>
  );
}
