import sys, json
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")
import app as w

# 1) 非管理员访问 /admin 页面
cm = w.app.test_client()
cm.post("/api/login", json={"email":"member@local.test","password":"member12345678"})
r = cm.get("/admin")
print("member GET /admin ->", r.status_code, r.headers.get("Location"))

# 2) 管理员访问
ca = w.app.test_client()
ca.post("/api/login", json={"email":"admin@local.test","password":"admin12345678"})
print("admin  GET /admin ->", ca.get("/admin").status_code)

# 3) 未登录访问
print("anon   GET /admin ->", w.app.test_client().get("/admin").status_code,
      w.app.test_client().get("/admin").headers.get("Location"))

# 4) AI 配置接口
print("member GET /api/admin/ai ->", cm.get("/api/admin/ai").status_code)
print("admin  GET /api/admin/ai ->", ca.get("/api/admin/ai").get_json())
print("test   /api/admin/ai/test (no key) ->", ca.post("/api/admin/ai/test").get_json())

# 5) 保存（不设 key，只开开关）
print("save   ->", ca.post("/api/admin/ai", json={"enabled": True, "base_url":"https://api.deepseek.com/v1", "model":"deepseek-chat"}).get_json())
print("admin  GET /api/admin/ai ->", ca.get("/api/admin/ai").get_json())
