// 查询最近2次用户评分结果
db.user_article_scores.find({}).sort({scored_at: -1}).limit(2).forEach(d => {
  print("=== User Score ===");
  print("url_hash: " + d.url_hash);
  print("product_relevance: " + d.product_relevance);
  print("event_impact: " + d.event_impact);
  print("pr_total_score: " + d.pr_total_score);
  print("score_reason: " + d.score_reason);
  print("product_scores: " + JSON.stringify(d.product_scores));
  print("scored_at: " + d.scored_at);
  print("");
});
