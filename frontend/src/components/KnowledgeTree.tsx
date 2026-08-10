/**
 * 知识库目录树组件
 *
 * 将后端返回的 KnowledgeTreeNode[] 转换为 Ant Design Tree 格式，
 * 支持文件 / 文件夹图标区分，以及核心打分文件高亮。
 */

import { FileOutlined, FolderOutlined, StarOutlined } from '@ant-design/icons';
import { Tree, type TreeDataNode } from 'antd';
import { useMemo } from 'react';
import type { KnowledgeTreeNode } from '../types';

interface KnowledgeTreeProps {
  treeData: KnowledgeTreeNode[];
  onSelect: (path: string) => void;
  /** 核心打分文件路径集合，用于在树中高亮标识 */
  directScoringPaths?: Set<string>;
  /** 当前选中的文件路径 */
  selectedPath?: string;
}

function toAntdTree(nodes: KnowledgeTreeNode[], directScoringPaths?: Set<string>): TreeDataNode[] {
  return nodes.map((node) => {
    const isFile = node.node_type === 'file';
    const isDirect = isFile && directScoringPaths?.has(node.path);
    const title = (
      <span key={node.path}>
        {node.name}
        {isDirect && (
          <StarOutlined
            style={{ color: '#faad14', marginLeft: 6, fontSize: 12 }}
            aria-label="核心打分文件"
          />
        )}
      </span>
    );

    return {
      key: node.path,
      title,
      icon: isFile ? <FileOutlined /> : <FolderOutlined />,
      isLeaf: isFile,
      children: node.children?.length ? toAntdTree(node.children, directScoringPaths) : undefined,
    };
  });
}

export default function KnowledgeTree({
  treeData,
  onSelect,
  directScoringPaths,
  selectedPath,
}: KnowledgeTreeProps) {
  const antdData = useMemo(
    () => toAntdTree(treeData, directScoringPaths),
    [treeData, directScoringPaths],
  );

  return (
    <Tree
      treeData={antdData}
      showIcon
      blockNode
      selectedKeys={selectedPath ? [selectedPath] : []}
      onSelect={(keys) => {
        if (keys.length > 0 && typeof keys[0] === 'string') {
          onSelect(keys[0]);
        }
      }}
      style={{ overflowX: 'auto', maxHeight: '60vh' }}
    />
  );
}
