import sys
sys.path.insert(0, "webapp"); sys.path.insert(0, ".")
import runner, ai

class FakeAI:
    def __init__(self, ids=None, ok=True):
        self.ids, self.ok, self.calls = ids or [], ok, 0
    def available(self): return self.ok
    def answer(self, stem, options):
        self.calls += 1
        self.last = (stem, options)
        return self.ids

def mk(answerer):
    f = runner.SmartFucker.__new__(runner.SmartFucker)
    f.answerer = answerer
    f.ai_answered = 0
    f.ai_missed = 0
    return f

# 分支 1：服务端下发了答案 → 直接选，问 AI 的次数必须是 0
fa = FakeAI(["999"])
f = mk(fa)
q = {"testQuestionName": "1+1=?",
     "questionOptions": [{"id": 11, "content": "2", "result": "1"},
                         {"id": 12, "content": "3", "result": "0"}]}
print("分支1 有答案 ->", repr(f.answerZhidao(q)), "| AI调用次数:", fa.calls, "| ai_answered:", f.ai_answered)

# 分支 2：答案缺失 → 交给 AI
fa2 = FakeAI(["12"])
f2 = mk(fa2)
q2 = {"testQuestionName": "<p>下列属于质数的是</p>",
      "questionOptions": [{"id": 11, "content": "<p>4</p>"},
                          {"id": 12, "content": "<p>7</p>"}]}
print("分支2 缺答案 ->", repr(f2.answerZhidao(q2)), "| AI调用:", fa2.calls,
      "| 题干已去标签:", fa2.last[0], "| 选项:", fa2.last[1], "| ai_answered:", f2.ai_answered)

# 分支 3：AI 未配置 → 返回空，不抛异常
f3 = mk(FakeAI(ok=False))
print("分支3 无AI   ->", repr(f3.answerZhidao(q2)), "| ai_missed:", f3.ai_missed)

# 分支 4：AI 给出不在选项里的 id（幻觉）→ 由 Answerer 过滤，这里模拟空返回
f4 = mk(FakeAI(["777"]))
print("分支4 幻觉id ->", repr(f4.answerZhidao(q2)), "（由上层校验，Answerer 只返回合法 id）")

# 分支 5：多选题多个答案
fa5 = FakeAI(["11", "12"])
f5 = mk(fa5)
q5 = {"testQuestionName": "多选",
      "questionOptions": [{"id": 11, "content": "a"}, {"id": 12, "content": "b"}]}
print("分支5 多选   ->", repr(f5.answerZhidao(q5)))
