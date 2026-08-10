/**
 * DraftBlockView - 草稿预览组件（支持鼠标拖选文本）
 *
 * 渲染 Markdown 草稿，用户可通过鼠标拖选任意文本片段，
 * 选中后显示"针对选中内容修改"按钮，回调通知父组件。
 */

import { EditOutlined } from '@ant-design/icons';
import { Button, Empty } from 'antd';
import { useCallback, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';

export interface DraftBlock {
  index: number;
  text: string;
  type: 'paragraph' | 'selection';
}

interface DraftBlockViewProps {
  content: string;
  selectedBlockIndex?: number | null;
  onSelectBlock?: (block: DraftBlock | null) => void;
}

export default function DraftBlockView({ content, onSelectBlock }: DraftBlockViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedText, setSelectedText] = useState<string | null>(null);
  const [selectionRect, setSelectionRect] = useState<{ top: number; left: number } | null>(null);

  const handleMouseUp = useCallback(() => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) {
      setSelectedText(null);
      setSelectionRect(null);
      return;
    }

    const text = selection.toString().trim();
    if (!text || text.length < 2) {
      setSelectedText(null);
      setSelectionRect(null);
      return;
    }

    // 获取选区位置用于定位按钮
    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    const containerRect = containerRef.current?.getBoundingClientRect();
    if (containerRect) {
      setSelectionRect({
        top: rect.top - containerRect.top - 36,
        left: rect.left - containerRect.left + rect.width / 2,
      });
    }

    setSelectedText(text);
  }, []);

  const handleSelectForEdit = useCallback(() => {
    if (selectedText) {
      onSelectBlock?.({ index: 0, text: selectedText, type: 'selection' });
      window.getSelection()?.removeAllRanges();
      setSelectedText(null);
      setSelectionRect(null);
    }
  }, [selectedText, onSelectBlock]);

  const handleClearSelection = useCallback(() => {
    window.getSelection()?.removeAllRanges();
    setSelectedText(null);
    setSelectionRect(null);
    onSelectBlock?.(null);
  }, [onSelectBlock]);

  if (!content) {
    return <Empty description="草稿内容不可用" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  return (
    <div
      ref={containerRef}
      onMouseUp={handleMouseUp}
      style={{ position: 'relative', lineHeight: 1.8, userSelect: 'text' }}
    >
      {/* 选中后浮出的操作按钮 */}
      {selectedText && selectionRect && (
        <div
          style={{
            position: 'absolute',
            top: Math.max(0, selectionRect.top),
            left: selectionRect.left,
            transform: 'translateX(-50%)',
            zIndex: 10,
            display: 'flex',
            gap: 4,
            background: '#1677ff',
            borderRadius: 6,
            padding: '4px 8px',
            boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
          }}
        >
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={handleSelectForEdit}
            style={{ color: '#fff', padding: 0, fontSize: 13 }}
          >
            针对选中内容修改
          </Button>
          <Button
            type="link"
            size="small"
            onClick={handleClearSelection}
            style={{ color: 'rgba(255,255,255,0.7)', padding: 0, fontSize: 13 }}
          >
            取消
          </Button>
        </div>
      )}

      <div className="markdownContent">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
