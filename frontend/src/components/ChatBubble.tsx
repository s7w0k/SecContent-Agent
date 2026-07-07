/**
 * 对话气泡组件
 * 
 * 支持：头像、圆角气泡、阴影、时间戳、消息内容渲染
 * 区分 user 和 assistant 角色样式
 */

import { UserOutlined, RobotOutlined } from "@ant-design/icons";
import styles from "./ChatBubble.module.css";
import type { ChatMessage } from "../types";

interface ChatBubbleProps {
  message: ChatMessage;
  index: number;
}

export default function ChatBubble({ message, index }: ChatBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={styles.bubbleContainer}
      style={{ justifyContent: isUser ? "flex-end" : "flex-start" }}
    >
      {/* 头像 */}
      <div className={styles.avatarWrapper}>
        <div className={styles.avatar} style={{ background: isUser ? "#1677ff" : "#52c41a" }}>
          {isUser ? (
            <UserOutlined style={{ color: "#fff" }} />
          ) : (
            <RobotOutlined style={{ color: "#fff" }} />
          )}
        </div>
      </div>

      {/* 气泡 */}
      <div className={styles.bubbleContent}>
        <div className={styles.bubble} style={{ background: isUser ? "#1677ff" : "#fff" }}>
          <div className={styles.messageText} style={{ color: isUser ? "#fff" : "#333" }}>
            {message.content}
          </div>
        </div>
        <div className={styles.timestamp}>
          {new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
        </div>
      </div>
    </div>
  );
}
