import { useCallback, useEffect, useState } from "react";
import { Card, Col, Layout, List, Menu, Row, Tag, Typography, Empty, Spin } from "antd";
import { CalendarOutlined, InfoCircleOutlined, WarningOutlined, CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import axios from "axios";

const { Sider, Content } = Layout;
const { Text, Title } = Typography;

const LEVEL_ICON: Record<string, any> = {
  INFO: <InfoCircleOutlined style={{ color: "#1677ff" }} />,
  WARNING: <WarningOutlined style={{ color: "#fa8c16" }} />,
  ERROR: <CloseCircleOutlined style={{ color: "#ff4d4f" }} />,
  SUCCESS: <CheckCircleOutlined style={{ color: "#52c41a" }} />,
};
const LEVEL_COLOR: Record<string, string> = { INFO: "blue", WARNING: "orange", ERROR: "red", SUCCESS: "green" };

export default function LogsPage() {
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>(new Date().toISOString().slice(0, 10));
  const [logs, setLogs] = useState<any[]>([]);
  const [phases, setPhases] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    axios.get("/api/logs/dates").then((r) => {
      const ds = r.data.dates || [];
      setDates(ds);
      if (ds.length > 0 && !ds.includes(selectedDate)) setSelectedDate(ds[0]);
    }).catch(() => {});
  }, []);

  const loadLogs = useCallback(async (date: string) => {
    setLoading(true);
    try {
      const r = await axios.get(`/api/logs/${date}`);
      setLogs(r.data.logs || []);
      setPhases(r.data.phases || []);
    } catch { setLogs([]); }
    setLoading(false);
  }, []);

  useEffect(() => { if (selectedDate) loadLogs(selectedDate); }, [selectedDate, loadLogs]);

  return (
    <Layout style={{ minHeight: "calc(100vh - 64px)", background: "#fff" }}>
      <Sider width={200} style={{ background: "#fafafa", borderRight: "1px solid #f0f0f0" }}>
        <div style={{ padding: "12px 16px", fontWeight: "bold", borderBottom: "1px solid #f0f0f0" }}>
          <CalendarOutlined /> 运行日期
        </div>
        <Menu
          mode="inline"
          style={{ background: "transparent", border: "none" }}
          selectedKeys={[selectedDate]}
          onClick={({ key }) => setSelectedDate(key)}
          items={dates.map((d) => ({ key: d, label: d }))}
        />
      </Sider>
      <Content style={{ padding: 16 }}>
        <Row justify="space-between" align="middle" style={{ marginBottom: 12 }}>
          <Title level={4} style={{ margin: 0 }}>{selectedDate} 运行日志</Title>
          <div>
            {phases.map((p) => <Tag key={p} color="blue">{p}</Tag>)}
            <Text type="secondary" style={{ marginLeft: 8 }}>{logs.length} entries</Text>
          </div>
        </Row>
        <Spin spinning={loading}>
          {logs.length === 0 ? <Empty description="暂无日志" /> : (
            <List
              size="small"
              dataSource={logs}
              renderItem={(item: any) => (
                <List.Item>
                  <List.Item.Meta
                    avatar={LEVEL_ICON[item.level] || LEVEL_ICON.INFO}
                    title={
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <Tag color={LEVEL_COLOR[item.level] || "blue"}>{item.level}</Tag>
                        <Tag>{item.phase}</Tag>
                        <Text style={{ fontSize: 12, color: "#999" }}>{item.created_at}</Text>
                      </div>
                    }
                    description={item.message}
                  />
                </List.Item>
              )}
            />
          )}
        </Spin>
      </Content>
    </Layout>
  );
}
