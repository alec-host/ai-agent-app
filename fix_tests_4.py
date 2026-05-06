import os
import re

for r, d, files in os.walk('tests'):
    for f in files:
        if f.endswith('.py'):
            filepath = os.path.join(r, f)
            with open(filepath, 'r', encoding='utf-8') as file:
                content = file.read()
            
            # For the pattern:
            # mock_services = {
            #     "calendar": type('obj', (object,), { ... })
            # }
            # Or similar with single quotes
            
            pattern = re.compile(r'mock_services\s*=\s*\{\s*[\'"]calendar[\'"]\s*:\s*(type\(\'obj\'.*?\}\))\s*\}', re.DOTALL)
            new_content = pattern.sub(r'mock_service_obj = \1\n    mock_services = {"calendar": mock_service_obj, "session": mock_service_obj}', content)
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as file:
                    file.write(new_content)
                print(f"Updated {filepath}")
