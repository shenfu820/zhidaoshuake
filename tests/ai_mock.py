import sys, json, threading, http.server, socketserver
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")

ANSWER = {"answer": [102]}
HITS = []

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HITS.append({"path": self.path, "auth": self.headers.get("Authorization"),
                     "model": body.get("model")})
        out = json.dumps({"choices": [{"message": {
            "content": "```answer\n" + json.dumps(ANSWER) + "\n```"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    def log_message(self, *a): pass

srv = socketserver.TCPServer(("127.0.0.1", 8790), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

import ai, app as w

# 指向假端点，验证：请求路径 / 鉴权头 / 模型名 / 解析 / 加密落库
ai.save({"enabled": True, "base_url": "http://127.0.0.1:8790", "model": "deepseek-chat",
         "api_key": "sk-mock-1234567890"})
a = ai.Answerer()
print("available:", a.available())
print("direct answer:", a.answer("下列哪个数是偶数？",
      [{"id": 101, "content": "3"}, {"id": 102, "content": "8"}, {"id": 103, "content": "5"}]))
print("mock saw:", HITS)

ca = w.app.test_client()
ca.post("/api/login", json={"email": "admin@local.test", "password": "admin12345678"})
print("admin test route:", ca.post("/api/admin/ai/test").get_json())
print("public view:", ca.get("/api/admin/ai").get_json())

# 兜底解析：模型输出乱七八糟的格式也要能挖出 id
cases = [
    '```answer\n[102]\n```',
    '{"answer": [102]}',
    '我认为是 8，也就是 [{"id": 102, "content": "8"}]',
    '102',
    '答案：\\n```json\\n{"answer":[102]}\\n```',
]
for c in cases:
    print("parse", repr(c[:34]), "->", ai.Answerer._extract_ids(c, {"101","102","103"}))

# 错误场景：模型答非所问 / 网络失败
srv.shutdown()
print("after shutdown answer:", a.answer("x", [{"id": 1, "content": "a"}]), "fallbacks:", a.fallbacks)

ai.save({"enabled": False, "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "clear_key": True})
print("restored:", ai.public_view())
