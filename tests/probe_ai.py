import sys, json
sys.path.insert(0, ".")
sys.path.insert(0, "webapp")
from fucker import Fucker

f = Fucker(cookies=json.load(open("cookies.json", encoding="utf-8")), proxies={},
           tree_view=False, progressbar_view=False)
try:
    ret = f.getZhidaoAiList()
    print("type:", type(ret).__name__)
    print(json.dumps(ret, ensure_ascii=False, indent=2)[:1500])
except Exception as e:
    print("AI list ERR:", type(e).__name__, e)
try:
    zs = f.getZhidaoList()
    print("zhidao count:", len(zs))
    if zs:
        c = zs[0]
        print("sample keys:", list(c.keys())[:12] if hasattr(c, "keys") else dir(c)[:12])
except Exception as e:
    print("zhidao ERR:", type(e).__name__, e)
