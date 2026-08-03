var d = db.llm_call_logs.findOne({agent_type: "scorer_v2"});
if (d) {
  print("degraded: " + d.degraded);
  if (d.result) print("result: " + JSON.stringify(d.result));
  if (d.raw_response) print("raw: " + JSON.stringify(d.raw_response).substring(0, 800));
} else {
  print("no scorer_v2 logs found");
}
