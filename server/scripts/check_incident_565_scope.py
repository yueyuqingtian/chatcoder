"""plan-565-2323 收尾影响面核对（只读）。

目的：确认 repair_stale_rows 收窄到「子代理任务」是否足够解锁事故会话，
以及 166 条 pending 里有多少是子代理任务（决定是否会大面积改写历史任务状态）。
"""
import sqlite3

DB = r"C:/Users/yueyuqingtian/AppData/Local/chatcoder/chatcoder.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

print("== 轮次已结束、仍非终态的 task，按『是否子代理任务』分 ==")
for row in cur.execute(
    "select t.status, "
    "       case when a.kind='sub' then 'sub' else coalesce(a.kind,'no_agent') end as who, "
    "       count(*) "
    "from tasks t "
    "join turns tu on tu.id = t.turn_id "
    "left join agents a on a.id = t.agent_id "
    "where t.status in ('proposed','pending','running','in_progress') and tu.status <> 'running' "
    "group by t.status, who order by t.status, who"
):
    print("  ", row)

print()
print("== 前端会算作「子代理运行中」的条目（agents.kind='sub' 且 task 非终态、turn 已结束）==")
for row in cur.execute(
    "select a.session_id, a.id, a.status, t.status "
    "from agents a join turns tu on tu.id = a.turn_id "
    "left join tasks t on t.agent_id = a.id "
    "where a.kind='sub' and tu.status <> 'running' "
    "  and (t.status in ('proposed','pending','running','in_progress') or a.status='running')"
):
    print("  ", row)

print()
print("== 子代理任务总数（agents.kind='sub'）==")
print("  ", cur.execute(
    "select count(*) from tasks t join agents a on a.id=t.agent_id where a.kind='sub'"
).fetchone())

con.close()
