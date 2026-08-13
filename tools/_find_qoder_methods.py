"""Find all JSON-RPC method names in Qoder CN extension."""
import re
data = open(r'C:\Program Files\QoderCN\resources\app\extensions\aicoding-agent\dist\extension.js', 'r', encoding='utf-8', errors='replace').read()

# Find all "xxx/yyy" strings that look like JSON-RPC methods
methods = set()
for m in re.finditer(r'"([a-z][a-z_]*/[a-z_]+)"', data):
    name = m.group(1)
    if not name.startswith('http') and not name.startswith('text/'):
        methods.add(name)

for name in sorted(methods):
    print(f"  {name}")
