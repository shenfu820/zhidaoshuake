import sys
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")
import app as webapp

def login(email, pw):
    c = webapp.app.test_client()
    r = c.post("/api/login", json={"email": email, "password": pw})
    return c, r.status_code

cm, code = login("member@local.test", "member12345678")
if code != 200:
    cm = webapp.app.test_client()
    cm.post("/api/register", json={"email": "member@local.test", "password": "member12345678"})
me = cm.get("/api/me").get_json()
print("member login:", code, "| role:", me["role"])
bad = []
for p in ["/api/admin/overview", "/api/admin/users", "/api/admin/tasks",
          "/api/admin/audit", "/api/admin/settings", "/api/admin/ai"]:
    s = cm.get(p).status_code
    if s != 403: bad.append((p, s))
print("admin GET 全 403:", not bad, bad or "")
print("admin POST settings:", cm.post("/api/admin/settings", json={"max_concurrent": 9}).status_code)
print("admin POST ai:", cm.post("/api/admin/ai", json={"enabled": True}).status_code)
print("admin POST ai/test:", cm.post("/api/admin/ai/test").status_code)
print("页面 /admin:", cm.get("/admin").status_code, cm.get("/admin").headers.get("Location"))

ca, _ = login("admin@local.test", "admin12345678")
print("admin 全通:", [ca.get(p).status_code for p in
      ["/api/admin/overview", "/api/admin/users", "/api/admin/tasks",
       "/api/admin/audit", "/api/admin/settings", "/api/admin/ai"]])
print("admin /admin 页面:", ca.get("/admin").status_code)
