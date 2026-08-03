db.user_knowledge_entries.find({}).limit(3).forEach(d => printjson(d));
