import { useState } from "react";
import { Layout, Menu, Typography } from "antd";
import {
  DashboardOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import Dashboard from "./pages/Dashboard";

const { Header, Content } = Layout;
const { Text } = Typography;

const menuItems = [
  { key: "dashboard", icon: <DashboardOutlined />, label: "仪表盘" },
  { key: "about", icon: <InfoCircleOutlined />, label: "关于" },
];

function App() {
  const [tab, setTab] = useState("dashboard");

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          background: "#001529",
          padding: "0 24px",
        }}
      >
        <Text
          strong
          style={{ color: "#fff", fontSize: 18, marginRight: 32 }}
        >
          🛡 PR Agent
        </Text>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[tab]}
          onClick={({ key }) => setTab(key)}
          items={menuItems}
          style={{ flex: 1, minWidth: 0 }}
        />
      </Header>
      <Content>
        {tab === "dashboard" && <Dashboard />}
        {tab === "about" && (
          <div style={{ padding: 48, maxWidth: 800, margin: "0 auto" }}>
            <h2>🚀 PR Agent Dashboard</h2>
            <p>智能体安全PR情报Agent系统 — 阶段一至三已完成</p>
            <ul>
              <li>MongoDB — 数据持久化</li>
              <li>mcp-wewe — 微信公众号 RSS 桥接</li>
              <li>mcp-crawl — 海外安全新闻爬虫桥接</li>
              <li>Backend — FastAPI + Agent Pipeline + REST API</li>
              <li>Frontend — React + Ant Design 仪表盘</li>
            </ul>
          </div>
        )}
      </Content>
    </Layout>
  );
}

export default App;
