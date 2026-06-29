import { Typography } from "antd";

const { Title, Paragraph } = Typography;

function App() {
  return (
    <div style={{ padding: 48, maxWidth: 800, margin: "0 auto" }}>
      <Title level={2}>🚀 PR Agent Dashboard</Title>
      <Paragraph type="secondary">智能体安全PR情报Agent系统 — 阶段一基础架构已就绪</Paragraph>
      <ul>
        <li>MongoDB — 数据持久化</li>
        <li>mcp-wewe — 微信公众号 RSS 桥接</li>
        <li>mcp-crawl — 海外安全新闻爬虫桥接</li>
        <li>Backend — FastAPI + Agent Pipeline</li>
      </ul>
    </div>
  );
}

export default App;
