"""plan-565-2323 事故现场只读核对（不写库）。

用途：确认用户库里确实存在「所属轮次已结束、任务仍非终态」的残留，
即启动收尾（repair_stale_rows）在升级后会把它收掉，会话随之解锁。
只读打开（mode=ro），任何写操作都会被 sqlite 拒绝。
"""
import sqlite3

DB = r"C:/Users/yueyuqingtian/AppData/Local/chatcoder/chatcoder.db"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

print("== 未结束的子任务（按状态）==")
for row in cur.execute(
    "select status, count(*) from tasks "
    "where status in ('proposed','pending','running','in_progress') group by status"
):
    print("  ", row)

print("== 其中所属轮次已结束的（本方案会被启动收尾清掉）==")
for row in cur.execute(
    "select t.status, count(*) from tasks t join turns tu on tu.id = t.turn_id "
    "where t.status in ('proposed','pending','running','in_progress') and tu.status <> 'running' "
    "group by t.status"
):
    print("  ", row)

print("== 所属轮次已结束、agent 仍 idle/running ==")
for row in cur.execute(
    "select a.status, count(*) from agents a join turns tu on tu.id = a.turn_id "
    "where a.status in ('idle','running') and tu.status <> 'running' group by a.status"
):
    print("  ", row)

print("== 受影响会话（最多 10 条）==")
for row in cur.execute(
    "select tu.session_id, count(*) from tasks t join turns tu on tu.id = t.turn_id "
    "where t.status in ('proposed','pending','running','in_progress') and tu.status <> 'running' "
    "group by tu.session_id order by 2 desc limit 10"
):
    print("  ", row)

print("== workbuddy 凭据现状 ==")
for row in cur.execute(
    "select p.name, c.id, c.label, c.is_active, c.status from provider_credentials c "
    "join providers p on p.id = c.provider_id "
    "where p.api_format = 'workbuddy' order by p.id, c.priority"
):
    print("  ", row)

con.close()
