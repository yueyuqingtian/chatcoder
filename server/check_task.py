"""检查 task 18 执行详情"""
import sys, json, urllib.request
from collections import Counter
sys.stdout.reconfigure(encoding='utf-8')

url = 'http://127.0.0.1:8000/api/sessions/2/threads/18/messages'
data = json.loads(urllib.request.urlopen(url).read())

print(f'Total messages: {len(data)}')

# Count tool types
tools = Counter()
for m in data:
    c = m.get('content', {})
    if isinstance(c, dict) and c.get('tool'):
        tools[c['tool']] += 1

print(f'Tool call counts: {dict(tools)}')

# Show text messages
print('\n=== Text messages ===')
for m in data:
    mt = m.get('msg_type', '')
    if mt == 'text':
        c = m.get('content', {})
        txt = c.get('text', '') if isinstance(c, dict) else str(c)
        print(f'[TEXT] {txt[:300]}')

# Show last 5 messages
print('\n=== Last 5 messages ===')
for m in data[-5:]:
    mt = m.get('msg_type', '')
    c = m.get('content', {})
    if isinstance(c, dict):
        txt = json.dumps(c, ensure_ascii=False)[:300]
    else:
        txt = str(c)[:300]
    print(f'[{mt}] {txt}')
    print()
