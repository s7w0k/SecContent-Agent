// 查询最近2次 scorer_v2 LLM 调用
db.llm_call_logs.find({agent_type: "scorer_v2"}).sort({created_at: -1}).limit(2).forEach(d => {
  print("=== LLM Call: " + d.call_id + " ===");
  print("created_at: " + d.created_at);
  print("degraded: " + d.degraded);
  print("degrade_reason: " + d.degrade_reason);
  print("system_prompt_hash: " + d.system_prompt_hash);
  print("input_tokens: " + d.input_tokens);
  print("output_tokens: " + d.output_tokens);
  print("duration_ms: " + d.duration_ms);
  print("structured_output: " + d.structured_output);
  print("");
});
