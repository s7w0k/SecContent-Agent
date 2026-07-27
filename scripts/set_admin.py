"""临时脚本：查看用户并设置管理员权限。"""
import asyncio
import os

from db.mongo import MongoDB


async def main():
    await MongoDB.connect(
        uri=os.environ.get("MONGO_URI", "mongodb://admin:pr_agent_2024@mongodb:27017"),
        db_name=os.environ.get("MONGO_DB", "pr_agent"),
    )
    users = await MongoDB.get_collection("users").find({}, {"username": 1, "is_admin": 1}).to_list(10)
    print("=== 现有用户 ===")
    for u in users:
        print(f"  {u.get('username')}  is_admin={u.get('is_admin', False)}")

    # 将所有用户设为管理员
    result = await MongoDB.get_collection("users").update_many(
        {"is_admin": {"$ne": True}},
        {"$set": {"is_admin": True}},
    )
    print(f"\n已将 {result.modified_count} 个用户设为管理员")

    # 验证
    users = await MongoDB.get_collection("users").find({}, {"username": 1, "is_admin": 1}).to_list(10)
    print("\n=== 更新后 ===")
    for u in users:
        print(f"  {u.get('username')}  is_admin={u.get('is_admin', False)}")

    await MongoDB.disconnect()


asyncio.run(main())
