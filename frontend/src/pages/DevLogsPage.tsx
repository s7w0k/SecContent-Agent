import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  InfoCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Timeline,
  Typography,
  message,
} from 'antd';
import type { TableColumnsType } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import { devLogsApi } from '../api/client';
import type {
  DevLogEntry,
  DevLogQuery,
  DevLogQueryResult,
  DevLogStats,
  DevLogTrace,
} from '../types';
import styles from './DevLogsPage.module.css';

const { Text, Title } = Typography;
const DEFAULT_PAGE_SIZE = 50;

interface FilterForm {
  date: Dayjs;
  user_id?: string;
  phase?: string[];
  level?: string[];
  trace_id?: string;
  keyword?: string;
}

const LEVEL_ICON: Record<string, ReactNode> = {
  INFO: <InfoCircleOutlined style={{ color: '#1677ff' }} />,
  WARNING: <WarningOutlined style={{ color: '#fa8c16' }} />,
  ERROR: <CloseCircleOutlined style={{ color: '#ff4d4f' }} />,
  CRITICAL: <CloseCircleOutlined style={{ color: '#cf1322' }} />,
  SUCCESS: <CheckCircleOutlined style={{ color: '#52c41a' }} />,
};

const LEVEL_COLOR: Record<string, string> = {
  INFO: 'blue',
  WARNING: 'orange',
  ERROR: 'red',
  CRITICAL: 'red',
  SUCCESS: 'green',
};

function jsonDetail(log: DevLogEntry): string {
  return JSON.stringify({ detail: log.detail, error: log.error }, null, 2);
}

export default function DevLogsPage() {
  const [form] = Form.useForm<FilterForm>();
  const [query, setQuery] = useState<DevLogQuery>({
    date: dayjs().format('YYYY-MM-DD'),
    page: 1,
    page_size: DEFAULT_PAGE_SIZE,
  });
  const [result, setResult] = useState<DevLogQueryResult>({
    logs: [],
    phases: [],
    levels: [],
    users: [],
    total: 0,
    page: 1,
    page_size: DEFAULT_PAGE_SIZE,
  });
  const [dates, setDates] = useState<string[]>([]);
  const [stats, setStats] = useState<DevLogStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [traceLoading, setTraceLoading] = useState(false);
  const [trace, setTrace] = useState<DevLogTrace | null>(null);

  const loadLogs = useCallback(async (nextQuery: DevLogQuery) => {
    setLoading(true);
    try {
      const [nextResult, nextStats] = await Promise.all([
        devLogsApi.query(nextQuery),
        devLogsApi.stats(nextQuery.date),
      ]);
      setResult(nextResult);
      setStats(nextStats);
    } catch {
      message.error('开发者日志加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLogs(query);
  }, [loadLogs, query]);

  useEffect(() => {
    devLogsApi
      .dates()
      .then(setDates)
      .catch(() => setDates([]));
  }, []);

  const submitFilters = (values: FilterForm) => {
    setQuery({
      date: values.date.format('YYYY-MM-DD'),
      user_id: values.user_id,
      phase: values.phase,
      level: values.level,
      trace_id: values.trace_id?.trim() || undefined,
      keyword: values.keyword?.trim() || undefined,
      page: 1,
      page_size: query.page_size,
    });
  };

  const resetFilters = () => {
    const date = dayjs();
    form.resetFields();
    form.setFieldValue('date', date);
    setQuery({ date: date.format('YYYY-MM-DD'), page: 1, page_size: DEFAULT_PAGE_SIZE });
  };

  const openTrace = useCallback(async (traceId: string) => {
    setTraceLoading(true);
    try {
      setTrace(await devLogsApi.trace(traceId));
    } catch {
      message.error('Trace 链路加载失败');
    } finally {
      setTraceLoading(false);
    }
  }, []);

  const columns = useMemo<TableColumnsType<DevLogEntry>>(
    () => [
      {
        title: '级别',
        dataIndex: 'level',
        width: 110,
        render: (level: string) => (
          <Tag icon={LEVEL_ICON[level]} color={LEVEL_COLOR[level] || 'default'}>
            {level}
          </Tag>
        ),
      },
      {
        title: '阶段',
        dataIndex: 'phase',
        width: 130,
        render: (phase: string) => <Tag>{phase}</Tag>,
      },
      {
        title: '用户',
        width: 150,
        render: (_, log) => (
          <Space direction="vertical" size={0}>
            <Text>{log.username || log.user_id}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {log.user_id}
            </Text>
          </Space>
        ),
      },
      { title: '动作', dataIndex: 'action', width: 110 },
      { title: '消息', dataIndex: 'message', ellipsis: true },
      {
        title: 'Trace ID',
        dataIndex: 'trace_id',
        width: 210,
        render: (traceId?: string) =>
          traceId ? (
            <Button
              type="link"
              size="small"
              loading={traceLoading}
              onClick={() => openTrace(traceId)}
            >
              {traceId}
            </Button>
          ) : (
            '-'
          ),
      },
      {
        title: '耗时',
        dataIndex: 'duration_ms',
        width: 100,
        render: (duration?: number) => (duration == null ? '-' : `${duration} ms`),
      },
      {
        title: '时间',
        dataIndex: 'created_at',
        width: 190,
        render: (value: string) => dayjs(value).format('YYYY-MM-DD HH:mm:ss'),
      },
    ],
    [openTrace, traceLoading],
  );

  return (
    <div style={{ padding: 24 }}>
      <Title level={3}>开发者日志</Title>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Card size="small">
            <Statistic title="日志总数" value={stats?.total || 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic
              title="错误数量"
              value={stats?.error_count || 0}
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="涉及用户" value={stats?.by_user.length || 0} />
          </Card>
        </Col>
      </Row>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Form<FilterForm>
          form={form}
          layout="inline"
          initialValues={{ date: dayjs() }}
          onFinish={submitFilters}
          style={{ rowGap: 12 }}
        >
          <Form.Item name="date" label="日期" rules={[{ required: true }]}>
            <DatePicker presets={dates.map((date) => ({ label: date, value: dayjs(date) }))} />
          </Form.Item>
          <Form.Item name="user_id" label="用户">
            <Select
              allowClear
              showSearch
              placeholder="全部用户"
              style={{ width: 160 }}
              options={result.users.map((user) => ({
                label: `${user.username} (${user.user_id})`,
                value: user.user_id,
              }))}
            />
          </Form.Item>
          <Form.Item name="phase" label="阶段">
            <Select
              mode="multiple"
              allowClear
              placeholder="全部阶段"
              style={{ minWidth: 160 }}
              options={result.phases.map((phase) => ({ label: phase, value: phase }))}
            />
          </Form.Item>
          <Form.Item name="level" label="级别">
            <Select
              mode="multiple"
              allowClear
              placeholder="全部级别"
              style={{ minWidth: 160 }}
              options={result.levels.map((level) => ({ label: level, value: level }))}
            />
          </Form.Item>
          <Form.Item name="trace_id" label="Trace ID">
            <Input allowClear placeholder="输入链路 ID" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item name="keyword" label="关键词">
            <Input allowClear placeholder="搜索消息" style={{ width: 180 }} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SearchOutlined />}>
                查询
              </Button>
              <Button icon={<ReloadOutlined />} onClick={resetFilters}>
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      <Table<DevLogEntry>
        rowKey={(log) => log.log_id || log._id || `${log.trace_id}-${log.created_at}`}
        columns={columns}
        dataSource={result.logs}
        loading={loading}
        scroll={{ x: 1400 }}
        rowClassName={(log) => (['ERROR', 'CRITICAL'].includes(log.level) ? styles.errorRow : '')}
        expandable={{
          expandRowByClick: true,
          expandedRowRender: (log) => <pre className={styles.detail}>{jsonDetail(log)}</pre>,
        }}
        pagination={{
          current: result.page,
          pageSize: result.page_size,
          total: result.total,
          showSizeChanger: true,
          pageSizeOptions: [20, 50, 100, 200],
          showTotal: (total) => `共 ${total} 条`,
          onChange: (page, pageSize) =>
            setQuery((current) => ({ ...current, page, page_size: pageSize })),
        }}
      />

      <Modal
        title={`Trace 链路：${trace?.trace_id || ''}`}
        open={Boolean(trace)}
        onCancel={() => setTrace(null)}
        footer={null}
        width={900}
      >
        {trace && (
          <>
            <Descriptions size="small" column={4} style={{ marginBottom: 20 }}>
              <Descriptions.Item label="用户">{trace.username}</Descriptions.Item>
              <Descriptions.Item label="阶段数">{trace.phase_count}</Descriptions.Item>
              <Descriptions.Item label="总耗时">{trace.total_duration_ms} ms</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={trace.has_error ? 'red' : 'green'}>
                  {trace.has_error ? '存在错误' : '正常'}
                </Tag>
              </Descriptions.Item>
            </Descriptions>
            <Timeline
              items={trace.events.map((event) => ({
                color: ['ERROR', 'CRITICAL'].includes(event.level) ? 'red' : 'blue',
                children: (
                  <div>
                    <Space>
                      <Tag color={LEVEL_COLOR[event.level]}>{event.level}</Tag>
                      <Tag>{event.phase}</Tag>
                      <Text type="secondary">{dayjs(event.created_at).format('HH:mm:ss.SSS')}</Text>
                    </Space>
                    <div style={{ marginTop: 4 }}>{event.message}</div>
                    {event.duration_ms != null && (
                      <Text type="secondary">耗时：{event.duration_ms} ms</Text>
                    )}
                  </div>
                ),
              }))}
            />
          </>
        )}
      </Modal>
    </div>
  );
}
