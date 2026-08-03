print("total:", db.articles.countDocuments());
print("with_url_hash:", db.articles.countDocuments({url_hash:{$exists:true}}));
print("without_url_hash:", db.articles.countDocuments({url_hash:{$exists:false}}));
var dupes = db.articles.aggregate([
  {$group:{_id:"$url_hash",count:{$sum:1}}},
  {$match:{count:{$gt:1}}}
]).toArray();
print("dup_groups:", dupes.length);
if (dupes.length > 0) {
  printjson(dupes.slice(0, 5));
}
