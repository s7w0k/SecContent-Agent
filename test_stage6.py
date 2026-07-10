"""阶段六新功能测试脚本 - 用户反馈与个性化风格学习"""
import httpx
import json
import sys

BASE = "http://localhost:8000/api"

def pretty(label, resp):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        print(resp.text)
    print()

# ── 1. 健康检查 ──
print("\n" + "🔴"*30)
print("  阶段六功能测试 - 用户反馈与个性化风格学习")
print("🔴"*30)

r = httpx.get(f"{BASE}/health")
pretty("1. 健康检查", r)

# ── 2. 查找有 PR 草稿的文章 ──
r = httpx.get(f"{BASE}/articles", params={"page": 1, "page_size": 50})
data = r.json()
articles_with_drafts = [a for a in data["items"] if a.get("pr_drafts")]

print(f"\n📋 文章总数: {data['total']}, 有草稿的: {len(articles_with_drafts)}")

if not articles_with_drafts:
    print("\n⚠️  没有带 PR 草稿的文章，请先运行流水线生成草稿：")
    print("   POST /api/pipeline/run-v2")
    print("   或在前端仪表盘点击「触发流水线」")
    sys.exit(0)

article = articles_with_drafts[0]
url_hash = article["url_hash"]
drafts = article["pr_drafts"]
draft_index = 0

print(f"\n✅ 选用文章: {article['title'][:60]}")
print(f"   url_hash: {url_hash}")
print(f"   草稿数: {len(drafts)}, 使用 draft_index={draft_index}")
print(f"   模板: {drafts[draft_index].get('template')}, 视角: {drafts[draft_index].get('perspective')}")

# ── 3. 提交反馈 ──
print("\n" + "🔹"*30)
print("  测试: 提交草稿反馈")
print("🔹"*30)

feedback_payload = {
    "target_type": "draft",
    "target_ref": {
        "article_url_hash": url_hash,
        "draft_index": draft_index,
    },
    "rating": 5,
    "comment": "这版草稿角度很好，标题有冲击力，可以直接使用",
    "tags": ["结构清晰", "标题有冲击力"],
}
r = httpx.post(f"{BASE}/feedback", json=feedback_payload)
pretty("2. POST /api/feedback (提交反馈)", r)
feedback_id = r.json().get("data", {}).get("feedback_id", "")

# ── 4. 查询反馈 ──
r = httpx.get(f"{BASE}/feedback", params={
    "target_type": "draft",
    "article_url_hash": url_hash,
    "draft_index": draft_index,
})
pretty("3. GET /api/feedback (查询反馈)", r)

# ── 5. 反馈统计 ──
r = httpx.get(f"{BASE}/feedback/stats", params={"group_by": "template"})
pretty("4. GET /api/feedback/stats (反馈统计)", r)

# ── 6. 记录操作（模拟下载草稿） ──
print("\n" + "🔹"*30)
print("  测试: 记录用户操作")
print("🔹"*30)

activity_payload = {
    "action": "draft_download",
    "target": {
        "article_url_hash": url_hash,
        "draft_index": draft_index,
        "template": drafts[draft_index].get("template", ""),
        "perspective": drafts[draft_index].get("perspective", ""),
    },
    "context": {
        "article_title": article["title"],
        "category_v2": article.get("category_v2", ""),
        "pr_total_score": article.get("pr_total_score", 0),
    },
    "metadata": {
        "file_format": "md",
        "source_page": "test_script",
    },
}
r = httpx.post(f"{BASE}/activities/log", json=activity_payload)
pretty("5. POST /api/activities/log (记录下载操作)", r)

# ── 7. 记录更多操作（应用修订） ──
activity2 = {
    "action": "revision_apply",
    "target": {
        "article_url_hash": url_hash,
        "draft_index": draft_index,
        "template": drafts[draft_index].get("template", ""),
        "perspective": drafts[draft_index].get("perspective", ""),
    },
    "context": {"article_title": article["title"]},
}
r = httpx.post(f"{BASE}/activities/log", json=activity2)
pretty("6. POST /api/activities/log (记录应用修订)", r)

# ── 8. 查询操作记录 ──
r = httpx.get(f"{BASE}/activities", params={"page": 1, "page_size": 10})
pretty("7. GET /api/activities (查询操作记录)", r)

# ── 9. 操作统计 ──
r = httpx.get(f"{BASE}/activities/stats", params={"days": 30})
pretty("8. GET /api/activities/stats (操作统计)", r)

# ── 10. 获取用户风格画像 ──
print("\n" + "🔹"*30)
print("  测试: 用户风格画像")
print("🔹"*30)

r = httpx.get(f"{BASE}/profile/style")
if r.status_code == 404:
    print("\n📋 用户画像尚未生成（反馈数据不足），尝试手动重建...")
else:
    pretty("9. GET /api/profile/style (获取风格画像)", r)

# ── 11. 重建用户风格画像 ──
r = httpx.post(f"{BASE}/profile/rebuild")
pretty("10. POST /api/profile/rebuild (重建风格画像)", r)

# ── 12. 再次获取画像 ──
r = httpx.get(f"{BASE}/profile/style")
pretty("11. GET /api/profile/style (重建后获取画像)", r)

# ── 13. 更新反馈 ──
if feedback_id:
    print("\n" + "🔹"*30)
    print("  测试: 更新和删除反馈")
    print("🔹"*30)

    r = httpx.put(f"{BASE}/feedback/{feedback_id}", json={
        "rating": 4,
        "comment": "更新后的反馈：整体不错但篇幅偏长",
    })
    pretty(f"12. PUT /api/feedback/{feedback_id} (更新反馈)", r)

    r = httpx.delete(f"{BASE}/feedback/{feedback_id}")
    pretty(f"13. DELETE /api/feedback/{feedback_id} (删除反馈)", r)

# ── 总结 ──
print("\n" + "✅"*30)
print("  阶段六功能测试完成！")
print("✅"*30)
print("""
测试覆盖的 API 端点:
  ✅ POST   /api/feedback              - 提交反馈
  ✅ GET    /api/feedback              - 查询反馈
  ✅ GET    /api/feedback/stats        - 反馈统计
  ✅ PUT    /api/feedback/:id          - 更新反馈
  ✅ DELETE /api/feedback/:id          - 删除反馈
  ✅ POST   /api/activities/log        - 记录操作
  ✅ GET    /api/activities            - 查询操作记录
  ✅ GET    /api/activities/stats      - 操作统计
  ✅ GET    /api/profile/style         - 获取风格画像
  ✅ POST   /api/profile/rebuild       - 重建风格画像

前端测试:
  📌 打开 http://localhost:8000
  📌 仪表盘 -> 查看草稿 -> 评分反馈
  📌 对话改稿 -> 生成修订稿 -> 反馈
  📌 用户画像 -> 查看风格偏好和操作时间线
""")
