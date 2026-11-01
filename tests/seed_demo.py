import sys, json
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")
import store, sechelp
from urllib.parse import quote

uid = store.get_user_by_email("admin@local.test")["id"]
store.save_zhs(uid, "尾号8821", sechelp.encrypt(json.dumps([{
  "name":"CASLOGC","value":quote(json.dumps({"uuid":"seed00008821"})),
  "domain":".zhihuishu.com","port":None,"path":"/","expires":None,"secure":True,
  "rest":{"HttpOnly":None},"version":0,"comment":None,"comment_url":None,
  "rfc2099_only":False,"discard":True}])))

rows = [
  ("4e5f5b5f465c4859454a585958435c4259", "马克思主义基本原理", "zhidao", "running", 17, 72),
  ("a91c3d7e55b04f2e8c1d", "大学英语（三）", "zhidao", "done", 46, 46),
  ("7f2b19c4aa5e4d6b90f3", "线性代数", "zhidao", "need_captcha", 12, 38),
  ("D7C3E9A1B2F4", "形势与政策", "hike", "failed", 0, None),
]
for cid, name, kind, status, done, total in rows:
    tid = store.create_task(uid, cid, name, kind)
    store.update_task(tid, status=status, done=done, total=total,
                      log="12:40:02 共 %s 节，已完成 %s 节\n12:41:11 ▶ 第 %s 节\n12:43:26 ✔ 提交成功" % (total or 1, done, done or 1))
print("seeded", len(rows), "tasks for uid", uid)
