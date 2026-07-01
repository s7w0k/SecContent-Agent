import { useCallback, useEffect, useState } from "react";
import { Button, Card, Input, List, message, Popconfirm, Space, Tag, Typography } from "antd";
import { PlusOutlined, DeleteOutlined, ReloadOutlined, ApiOutlined } from "@ant-design/icons";
import axios from "axios";

const { Text, Title } = Typography;

interface Props {
  onCrawl: () => void;
}

export default function CrawlConfig({ onCrawl }: Props) {
  const [accounts, setAccounts] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(false);
  const [crawling, setCrawling] = useState(false);

  const loadAccounts = useCallback(async () => {
    try {
      const { data } = await axios.get("/api/crawl-config/accounts");
      setAccounts((data.accounts || []).map((a: any) => a.name));
    } catch { message.error("加载配置失败"); }
  }, []);

  useEffect(() => { loadAccounts(); }, [loadAccounts]);

  const addAccount = async () => {
    if (!newName.trim()) return;
    setLoading(true);
    try {
      await axios.post(`/api/crawl-config/accounts?name=${encodeURIComponent(newName.trim())}`);
      setNewName("");
      message.success("已添加");
      loadAccounts();
    } catch (e: any) { message.error(e?.response?.data?.detail || "添加失败"); }
    setLoading(false);
  };

  const delAccount = async (name: string) => {
    try {
      await axios.delete(`/api/crawl-config/accounts/${encodeURIComponent(name)}`);
      message.success("已删除");
      loadAccounts();
    } catch { message.error("删除失败"); }
  };

  const handleCrawl = async () => {
    setCrawling(true);
    try {
      const { data } = await axios.post("/api/pipeline/crawl-api", { days: 1 });
      message.success(`API 抓取完成: 新增 ${data.saved} 篇`);
      onCrawl();
    } catch (e: any) { message.error(`抓取失败: ${e?.response?.data?.detail || e.message}`); }
    setCrawling(false);
  };

  return (
    <Card
      title={<Space><ApiOutlined /><Text strong>API 抓取配置</Text></Space>}
      style={{ marginBottom: 16 }}
      extra={<Button icon={<ReloadOutlined />} onClick={loadAccounts} />}
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        {/* Crawl button + stats */}
        <Space>
          <Button type="primary" icon={<ApiOutlined />} onClick={handleCrawl} loading={crawling}>
            API 抓取
          </Button>
          <Text type="secondary">已配置 {accounts.length} 个公众号</Text>
        </Space>

        {/* Account list */}
        <div>
          <Text type="secondary" style={{ marginBottom: 8, display: "block" }}>抓取公众号列表</Text>
          <List
            size="small"
            bordered
            dataSource={accounts}
            locale={{ emptyText: "暂无配置" }}
            renderItem={(name: string) => (
              <List.Item
                actions={[
                  <Popconfirm title="确定删除？" onConfirm={() => delAccount(name)}>
                    <Button type="link" danger size="small" icon={<DeleteOutlined />} />
                  </Popconfirm>
                ]}
              >
                <Tag color="blue">{name}</Tag>
              </List.Item>
            )}
            style={{ maxHeight: 200, overflow: "auto" }}
          />
        </div>

        {/* Add form */}
        <Space>
          <Input
            placeholder="输入公众号名称"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onPressEnter={addAccount}
            style={{ width: 200 }}
          />
          <Button icon={<PlusOutlined />} onClick={addAccount} loading={loading}>添加</Button>
        </Space>
      </Space>
    </Card>
  );
}
