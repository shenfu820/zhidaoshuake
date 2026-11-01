import sys, json, time
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")
import app as w, store, ai

def cli():
    return w.app.test_client()

def show(tag, r, keys=None):
    j = r.get_json() if r.is_json else {}
    if keys: j = {k: j.get(k) for k in keys}
    print(f"{tag:<38} {r.status_code}  {json.dumps(j, ensure_ascii=False)[:150]}")
    return j

print("=== 1. 账号生命周期 ===")
c = cli()
c.post("/api/register", json={"email": "tmp1@local.test", "password": "oldpass12345"})
show("注册 tmp1", c.post("/api/login", json={"email": "tmp1@local.test", "password": "oldpass12345"}) or type("R",(object,),{"status_code":"?","is_json":False})())
r = c.post("/api/me/password", json={"old": "oldpass12345", "new": "newpass67890"})
show("改密码", r)
c2 = cli()
show("旧密码登录(应401)", c2.post("/api/login", json={"email": "tmp1@local.test", "password": "oldpass12345"}))
show("新密码登录", c2.post("/api/login", json={"email": "tmp1@local.test", "password": "newpass67890"}))

print("\n=== 2. 管理员对用户操作 ===")
a = cli(); a.post("/api/login", json={"email": "admin@local.test", "password": "admin12345678"})
uid = store.get_user_by_email("tmp1@local.test")["id"]
show("提升为 admin", a.post(f"/api/admin/users/{uid}/role", json={"role": "admin"}))
show("降回 user", a.post(f"/api/admin/users/{uid}/role", json={"role": "user"}))
show("删最后一个管理员(应被拒)", a.post(f"/api/admin/users/{a.get('/api/me').get_json()['id']}/role", json={"role": "user"}))
show("删除 tmp1", a.delete(f"/api/admin/users/{uid}"))
show("删后列表含 tmp1?", a.get("/api/admin/users"), )

print("\n=== 3. 任务生命周期（真课程，立刻停止） ===")
courses = a.get("/api/courses").get_json()
print("课程数:", len(courses.get("zhidao", [])))
target = courses["zhidao"][-1]
j = show("建任务", a.post("/api/tasks", json={"course_id": target["id"], "course_name": target["name"]}))
tid = j.get("task_id")
time.sleep(0.6)
show("详情", a.get(f"/api/tasks/{tid}?log=1"), ["status", "total", "done"])
show("停止", a.post(f"/api/tasks/{tid}/stop"))
time.sleep(1.2)
show("停止后详情", a.get(f"/api/tasks/{tid}"), ["status", "error"])

print("\n=== 4. 系统设置热更新 ===")
show("读设置", a.get("/api/admin/settings"), ["site_name", "max_concurrent", "max_tasks_per_user"])
show("改并发=5", a.post("/api/admin/settings", json={"max_concurrent": 5}))
show("确认", a.get("/api/admin/settings"), ["site_name", "max_concurrent"])
show("还原=3", a.post("/api/admin/settings", json={"max_concurrent": 3}))

print("\n=== 5. AI 配置 ===")
show("读", a.get("/api/admin/ai"), ["enabled", "model", "has_key"])
show("存(仅base/model)", a.post("/api/admin/ai", json={"enabled": False, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}))
print("  → base 规范化检查:", a.get("/api/admin/ai").get_json()["base_url"])
show("测试(无key应报错)", a.post("/api/admin/ai/test"))

print("\n=== 6. 绑定态 ===")
m = cli(); m.post("/api/login", json={"email": "member@local.test", "password": "member12345678"})
show("member 绑定态", m.get("/api/me"), ["zhs"])
show("member 解绑(本未绑定)", m.post("/api/zhs/unbind"))
show("member 拉课程(应400)", m.get("/api/courses"))
show("member 建任务(应400)", m.post("/api/tasks", json={"course_id": "x", "course_name": "y"}))
print("\n=== 7. 审计留痕 ===")
au = a.get("/api/admin/audit").get_json()
print("审计条数:", len(au), "| 最近动作:", [x["action"] for x in au[:8]])
