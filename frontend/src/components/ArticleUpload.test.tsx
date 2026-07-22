import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/client';
import ArticleUpload, { MAX_UPLOAD_SIZE, validateArticleFile } from './ArticleUpload';

vi.mock('../api/client', () => ({
  default: { uploadArticle: vi.fn() },
}));

describe('ArticleUpload', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders upload guidance and keeps submit disabled before selecting a file', () => {
    render(<ArticleUpload open onClose={vi.fn()} onUploaded={vi.fn()} />);

    expect(screen.getByText('上传文章')).toBeInTheDocument();
    expect(screen.getByText('点击或拖拽文件到此区域')).toBeInTheDocument();
    expect(screen.getByText(/支持 \.txt \/ \.md \/ \.pdf \/ \.docx/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传入库/ })).toBeDisabled();
  });

  it('validates extension and maximum size before upload', () => {
    expect(validateArticleFile({ name: 'article.exe', size: 100 })).toContain('仅支持');
    expect(validateArticleFile({ name: 'article.pdf', size: MAX_UPLOAD_SIZE + 1 })).toContain(
      '10MB',
    );
    expect(validateArticleFile({ name: 'article.docx', size: MAX_UPLOAD_SIZE })).toBeNull();
  });

  it('rejects an unsupported file in the upload interface', async () => {
    render(<ArticleUpload open onClose={vi.fn()} onUploaded={vi.fn()} />);
    const file = new File(['payload'], 'article.exe', { type: 'application/octet-stream' });
    const input = document.querySelector('input[type="file"]');

    expect(input).not.toBeNull();
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });

    expect(await screen.findByText('仅支持 .txt、.md、.pdf、.docx 文件')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传入库/ })).toBeDisabled();
    expect(api.uploadArticle).not.toHaveBeenCalled();
  });

  it('uploads the selected file with an optional title', async () => {
    vi.mocked(api.uploadArticle).mockResolvedValue({
      url_hash: 'hash-1',
      title: '自定义标题',
      source_type: 'user_upload',
      content_length: 120,
      message: '文章已入库',
    });
    const onClose = vi.fn();
    const onUploaded = vi.fn();
    render(<ArticleUpload open onClose={onClose} onUploaded={onUploaded} />);
    const file = new File(['这是一篇用于上传验证的文章内容。'.repeat(10)], 'article.md', {
      type: 'text/markdown',
    });

    fireEvent.change(screen.getByPlaceholderText('默认使用文件名作为标题'), {
      target: { value: '自定义标题' },
    });
    const input = document.querySelector('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole('button', { name: /上传入库/ }));

    await waitFor(() =>
      expect(api.uploadArticle).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'article.md' }),
        '自定义标题',
      ),
    );
    expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ url_hash: 'hash-1' }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('shows the backend error inside the modal', async () => {
    vi.mocked(api.uploadArticle).mockRejectedValue({
      response: { data: { error: { message: '该文件内容已上传过' } } },
    });
    render(<ArticleUpload open onClose={vi.fn()} onUploaded={vi.fn()} />);
    const file = new File(['有效文章内容'.repeat(20)], 'article.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]');
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole('button', { name: /上传入库/ }));

    expect(await screen.findByText('该文件内容已上传过')).toBeInTheDocument();
  });
});
