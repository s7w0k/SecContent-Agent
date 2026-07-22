import { InboxOutlined } from '@ant-design/icons';
import { Alert, Form, Input, Modal, Upload, message } from 'antd';
import type { UploadFile, UploadProps } from 'antd';
import { useEffect, useState } from 'react';
import api from '../api/client';
import type { UploadArticleResult } from '../types';

const { Dragger } = Upload;
export const MAX_UPLOAD_SIZE = 10 * 1024 * 1024;
export const ALLOWED_UPLOAD_EXTENSIONS = ['.txt', '.md', '.pdf', '.docx'];

export function validateArticleFile(file: Pick<File, 'name' | 'size'>): string | null {
  const extension = file.name.includes('.') ? `.${file.name.split('.').pop()?.toLowerCase()}` : '';
  if (!ALLOWED_UPLOAD_EXTENSIONS.includes(extension)) {
    return '仅支持 .txt、.md、.pdf、.docx 文件';
  }
  if (file.size > MAX_UPLOAD_SIZE) return '文件不能超过 10MB';
  return null;
}

function getUploadErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (
      error as {
        response?: { data?: { error?: { message?: unknown }; detail?: unknown } };
      }
    ).response;
    if (typeof response?.data?.error?.message === 'string') return response.data.error.message;
    if (typeof response?.data?.detail === 'string') return response.data.detail;
  }
  return error instanceof Error ? error.message : '上传失败，请稍后重试';
}

interface ArticleUploadProps {
  open: boolean;
  onClose: () => void;
  onUploaded: (result: UploadArticleResult) => void | Promise<void>;
}

export default function ArticleUpload({ open, onClose, onUploaded }: ArticleUploadProps) {
  const [title, setTitle] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setTitle('');
      setFile(null);
      setFileList([]);
      setError(null);
      setUploading(false);
    }
  }, [open]);

  const beforeUpload: UploadProps['beforeUpload'] = (nextFile) => {
    const validationError = validateArticleFile(nextFile);
    if (validationError) {
      setError(validationError);
      setFile(null);
      setFileList([]);
      return Upload.LIST_IGNORE;
    }
    setError(null);
    setFile(nextFile);
    setFileList([nextFile]);
    return false;
  };

  const handleUpload = async () => {
    if (!file) {
      setError('请选择需要上传的文章文件');
      return;
    }
    setUploading(true);
    setError(null);
    try {
      const result = await api.uploadArticle(file, title || undefined);
      message.success(result.message || '文章上传成功');
      await onUploaded(result);
      onClose();
    } catch (uploadError) {
      setError(getUploadErrorMessage(uploadError));
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal
      title="上传文章"
      open={open}
      okText="上传入库"
      cancelText="取消"
      confirmLoading={uploading}
      okButtonProps={{ disabled: !file }}
      onOk={handleUpload}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form layout="vertical">
        <Form.Item label="标题（可选）">
          <Input
            value={title}
            maxLength={500}
            placeholder="默认使用文件名作为标题"
            onChange={(event) => setTitle(event.target.value)}
          />
        </Form.Item>
        <Form.Item>
          <Dragger
            accept={ALLOWED_UPLOAD_EXTENSIONS.join(',')}
            beforeUpload={beforeUpload}
            fileList={fileList}
            maxCount={1}
            onRemove={() => {
              setFile(null);
              setFileList([]);
              return true;
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此区域</p>
            <p className="ant-upload-hint">支持 .txt / .md / .pdf / .docx，单文件不超过 10MB</p>
          </Dragger>
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon message={error} />}
    </Modal>
  );
}
