// MongoDB 初始化脚本
// 容器首次启动时自动执行（/docker-entrypoint-initdb.d/init.js）
// 创建数据库、集合和索引

db = db.getSiblingDB("pr_agent");

print("[init] Initializing pr_agent database...");

// ═══════════════════════════════════════════════
// 1. 创建集合
// ═══════════════════════════════════════════════
db.createCollection("articles");
db.createCollection("reports");
db.createCollection("knowledge_base");

print("[init] Collections created: articles, reports, knowledge_base");

// ═══════════════════════════════════════════════
// 2. articles 索引
// ═══════════════════════════════════════════════

// 唯一索引：URL 去重
db.articles.createIndex(
  { url_hash: 1 },
  { unique: true, name: "idx_url_hash" }
);

// 按来源 + 时间查询
db.articles.createIndex(
  { source_type: 1, added_at: -1 },
  { name: "idx_source_added" }
);

// 按综合分排序（仪表盘高分筛选）
db.articles.createIndex(
  { total_score: -1 },
  { name: "idx_total_score" }
);

// 按分类筛选
db.articles.createIndex(
  { category: 1 },
  { name: "idx_category" }
);

// 按入库时间倒序（列表默认排序）
db.articles.createIndex(
  { added_at: -1 },
  { name: "idx_added_at" }
);

// AI 安全标记联合查询
db.articles.createIndex(
  { is_ai_security: 1, is_agent_security: 1 },
  { name: "idx_ai_agent_security" }
);

// 发布时间范围查询
db.articles.createIndex(
  { published_at: -1 },
  { name: "idx_published_at" }
);

print("[init] articles indexes created (7)");

// ═══════════════════════════════════════════════
// 3. reports 索引
// ═══════════════════════════════════════════════

// 文章关联
db.reports.createIndex(
  { article_url_hash: 1 },
  { unique: true, name: "idx_article_hash" }
);

// 按创建时间倒序
db.reports.createIndex(
  { created_at: -1 },
  { name: "idx_created_at" }
);

print("[init] reports indexes created (2)");

// ═══════════════════════════════════════════════
// 4. knowledge_base 索引
// ═══════════════════════════════════════════════
db.knowledge_base.createIndex(
  { key: 1 },
  { unique: true, name: "idx_key" }
);

print("[init] knowledge_base indexes created (1)");
print("[init] Database pr_agent initialized successfully!");
