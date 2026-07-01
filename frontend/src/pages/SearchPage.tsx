import { useState } from "react";
import { Button, Card, message, Space, Table, Tag, Typography } from "antd";
import { SearchOutlined, ReloadOutlined } from "@ant-design/icons";
import axios from "axios";

const { Title, Text } = Typography;

const SITES = ["The Hacker News", "BleepingComputer", "SecurityWeek", "Help Net Security"];

export default function SearchPage() {
  const [results, setResults] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [savedCount, setSavedCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);

  const handleCrawl = async () => {
    setLoading(true);
    try {
      const { data } = await axios.get("/api/overseas/crawl", { params: { days: 2 } });
      setResults(data.results || []);
      setSavedCount(data.saved || 0);
      setTotalCount(data.total || 0);
      message.success(`${data.saved} new articles saved (${data.total} found, ${data.skipped} dupes)`);
    } catch (e: any) {
      message.error(`Crawl failed: ${e?.response?.data?.detail || e.message}`);
    }
    setLoading(false);
  };

  const columns = [
    { title: "Source", dataIndex: "source", key: "source", width: 150,
      render: (v: string) => <Tag color={v === "The Hacker News" ? "red" : "blue"}>{v}</Tag> },
    { title: "Title", dataIndex: "title", key: "title", ellipsis: true,
      render: (t: string, r: any) => <a href={r.url} target="_blank" rel="noopener noreferrer">{t}</a> },
    { title: "Date", dataIndex: "published_at", key: "date", width: 130,
      render: (v: string) => v?.slice(0, 16) || "-" },
  ];

  return (
    <div style={{ padding: 24, maxWidth: 1400, margin: "0 auto" }}>
      <Title level={3}>海外安全新闻爬取</Title>

      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space>
            <Button type="primary" icon={<SearchOutlined />} onClick={handleCrawl} loading={loading} size="large">
              Crawl Now
            </Button>
            <Button icon={<ReloadOutlined />} onClick={handleCrawl} loading={loading}>Refresh</Button>
            {totalCount > 0 && (
              <Text type="secondary">
                Found {totalCount} | Saved {savedCount} new to DB
              </Text>
            )}
          </Space>
          <Space wrap>
            {SITES.map((s) => <Tag key={s} color="processing">{s}</Tag>)}
          </Space>
        </Space>
      </Card>

      <Table
        columns={columns}
        dataSource={results}
        rowKey="url"
        loading={loading}
        size="small"
        pagination={{ pageSize: 20, showTotal: (t: number) => `${t} results` }}
        locale={{ emptyText: "Click Crawl Now to start crawling overseas security news" }}
      />
    </div>
  );
}
