/**
 * WeWe RSS account management page.
 *
 * Shows account login status, allows QR code login when expired.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Image,
  message,
  Modal,
  Button,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  ReloadOutlined,
  QrcodeOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import api from "../api/client";
import type { WeWeAccount } from "../types";

const { Title, Text } = Typography;
const POLL_INTERVAL = 5000; // 5s

export default function AccountPage() {
  const [accounts, setAccounts] = useState<WeWeAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [totalCount, setTotalCount] = useState(0);
  const [activeCount, setActiveCount] = useState(0);

  // QR code flow
  const [qrVisible, setQrVisible] = useState(false);
  const [qrImage, setQrImage] = useState("");
  const [qrUuid, setQrUuid] = useState("");
  const [polling, setPolling] = useState(false);
  const stopFlag = useRef(false);

  // ── Load account status ─────────────────────────

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getAccountStatus();
      if (res.ok && res.accounts) {
        setAccounts(res.accounts);
        setTotalCount(res.total ?? res.accounts.length);
        setActiveCount(res.active_count ?? 0);
      } else {
        message.warning(res.message || "Failed to load account status");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Unknown error";
      message.error(`Load failed: ${msg}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  // ── QR code flow ────────────────────────────────

  const stopPolling = useCallback(() => {
    stopFlag.current = true;
  }, []);

  const handleCreateQR = useCallback(async () => {
    try {
      const res = await api.createQRCode();
      const img = res.qr_base64 || res.qrcode_img || "";
      if (res.ok && res.uuid) {
        // use online QR API if no local base64
        setQrImage(img || `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(res.scan_url || "")}`);
        setQrUuid(res.uuid || "");
        setQrVisible(true);
        startPolling(res.uuid || "");
      } else {
        message.error(res.message || "Failed to create QR code");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Unknown error";
      message.error(`Create QR failed: ${msg}`);
    }
  }, []);

  const startPolling = useCallback(async (uuid: string) => {
    if (!uuid) return;
    stopFlag.current = false;
    setPolling(true);

    const poll = async () => {
      while (!stopFlag.current) {
        try {
          const res = await api.pollLogin(uuid, 10);
          if (res.status === "confirmed" && res.vid && res.token) {
            stopFlag.current = true;
            setPolling(false);
            setQrVisible(false);
            message.loading({ content: "Saving account...", key: "save", duration: 0 });
            const saveRes = await api.saveAccount(res.vid, res.token, res.name || "Unknown");
            if (saveRes.ok) {
              message.success({ content: "Account saved!", key: "save" });
              loadStatus();
            } else {
              message.error({ content: "Save failed", key: "save" });
            }
            return;
          } else if (res.status === "expired") {
            stopFlag.current = true;
            setPolling(false);
            message.warning("QR code expired, please try again");
            return;
          }
        } catch {
          // polling error, continue after delay
        }
        await new Promise((r) => setTimeout(r, POLL_INTERVAL));
      }
    };
    poll();
  }, [stopPolling, loadStatus]);

  const handleCloseQR = useCallback(() => {
    stopPolling();
    setPolling(false);
    setQrVisible(false);
    setQrImage("");
    setQrUuid("");
  }, [stopPolling]);

  // ── Table columns ────────────────────────────────

  const columns = [
    {
      title: "账号",
      dataIndex: "name",
      key: "name",
      width: 180,
    },
    {
      title: "ID",
      dataIndex: "id",
      key: "id",
      width: 100,
      ellipsis: true,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: any, record: any) => {
        // WeWe status: 1=normal/active, 0=expired, 2=disabled
        const label = record.status_label || (status === 1 ? "正常" : status === 0 ? "失效" : "禁用");
        const isActive = status === 1 || status === "active";
        const color = isActive ? "green" : status === 2 ? "orange" : "red";
        const icon = isActive ? <CheckCircleOutlined /> : <CloseCircleOutlined />;
        return <Tag color={color} icon={icon}>{label}</Tag>;
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 120,
      render: (_: any, record: any) => {
        const isActive = record.status === 1 || record.status_code === 1;
        return (
          <Space size="small">
            <Button type="link" size="small" danger={isActive}
              onClick={async () => {
                const ns = isActive ? 0 : 1;
                try { await api.toggleAccount(record.id, ns); message.success(isActive ? "disabled" : "enabled"); loadStatus(); }
                catch { message.error("failed"); }
              }}>{isActive ? "停用" : "启用"}</Button>
            <Popconfirm title="确定删除此账号？" onConfirm={async () => {
              try { await api.deleteAccount(record.id); message.success("deleted"); loadStatus(); }
              catch { message.error("delete failed"); }
            }}>
              <Button type="link" size="small" danger>删除</Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <Title level={3}>📱 公众号账号管理</Title>

      <Card style={{ marginBottom: 16 }}>
        <Space size="large">
          <Text strong>总账号: {totalCount}</Text>
          <Text strong style={{ color: "#52c41a" }}>正常: {activeCount}</Text>
          <Text strong style={{ color: "#ff4d4f" }}>失效: {totalCount - activeCount}</Text>
        </Space>
        <Space style={{ marginLeft: 24 }}>
          <Button icon={<ReloadOutlined />} onClick={loadStatus} loading={loading}>
            刷新状态
          </Button>
          <Button icon={<CloudDownloadOutlined />} onClick={async () => {
            try {
              const res: any = await api.refreshArticles();
              if (res.ok) {
                message.success(res.message || "更新指令已发送");
              } else {
                message.warning(res.message || "更新失败");
              }
            } catch {
              message.error("更新失败");
            }
          }}>
            更新全部
          </Button>
          <Button type="primary" icon={<QrcodeOutlined />} onClick={handleCreateQR}>
            添加账号
          </Button>
        </Space>
      </Card>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        pagination={false}
        locale={{ emptyText: "暂无账号数据" }}
      />

      {/* QR Code Modal */}
      <Modal
        title="扫码登录"
        open={qrVisible}
        onCancel={handleCloseQR}
        footer={null}
        width={400}
      >
        <div style={{ textAlign: "center" }}>
          {qrImage ? (
            <>
              <Image src={qrImage.startsWith("http") ? qrImage : `data:image/png;base64,${qrImage}`} alt="QR Code" />
              <div style={{ marginTop: 16 }}>
                {polling ? (
                  <Spin tip="等待扫码中..." indicator={<SyncOutlined spin />} />
                ) : (
                  <Text type="secondary">请使用微信扫描二维码</Text>
                )}
              </div>
            </>
          ) : (
            <Spin />
          )}
          <Alert
            type="info"
            message="打开微信 → 扫一扫 → 确认登录"
            style={{ marginTop: 12 }}
          />
        </div>
      </Modal>
    </div>
  );
}
