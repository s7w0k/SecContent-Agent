import { ClockCircleOutlined, LoadingOutlined } from '@ant-design/icons';
import { Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import styles from './LiveOperationProgress.module.css';

const { Text } = Typography;

interface LiveOperationProgressProps {
  label: string;
  message: string;
  startedAt: number;
}

function formatElapsed(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}分${seconds.toString().padStart(2, '0')}秒` : `${seconds}秒`;
}

export default function LiveOperationProgress({
  label,
  message,
  startedAt,
}: LiveOperationProgressProps) {
  const [elapsedSeconds, setElapsedSeconds] = useState(() =>
    Math.max(0, Math.floor((Date.now() - startedAt) / 1000)),
  );

  useEffect(() => {
    const updateElapsed = () => {
      setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    };
    updateElapsed();
    const timer = setInterval(updateElapsed, 1000);
    return () => clearInterval(timer);
  }, [startedAt]);

  return (
    <section className={styles.container} aria-label={`${label}执行进度`} aria-live="polite">
      <div className={styles.header}>
        <Space>
          <LoadingOutlined spin style={{ color: '#1677ff' }} />
          <Text strong>{label}</Text>
          <Tag color="processing">执行中</Tag>
        </Space>
        <Text type="secondary">
          <ClockCircleOutlined /> 已耗时 {formatElapsed(elapsedSeconds)}
        </Text>
      </div>
      <div className={styles.bar} aria-hidden="true" />
      <div className={styles.detail}>
        <Text>{message}</Text>
        <Text type="secondary">正在实时等待服务结果</Text>
      </div>
    </section>
  );
}
