import os
import re

for r, d, files in os.walk('tests'):
    for f in files:
        if f.endswith('.py'):
            filepath = os.path.join(r, f)
            with open(filepath, 'r', encoding='utf-8') as file:
                content = file.read()
            
            # Pattern: {"calendar": mock_something} -> {"calendar": mock_something, "session": mock_something}
            new_content = re.sub(r'\"calendar\":\s*(mock_[a-zA-Z0-9_]+)', r'"calendar": \1, "session": \1', content)
            new_content = re.sub(r'\'calendar\':\s*(mock_[a-zA-Z0-9_]+)', r"'calendar': \1, 'session': \1", new_content)
            
            # There might be `services['calendar']` where we should inject `services['session']`
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as file:
                    file.write(new_content)
                print(f"Updated {filepath}")
