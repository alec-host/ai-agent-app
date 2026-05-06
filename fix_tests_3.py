import os
import re

for r, d, files in os.walk('tests'):
    for f in files:
        if f.endswith('.py'):
            filepath = os.path.join(r, f)
            with open(filepath, 'r', encoding='utf-8') as file:
                content = file.read()
            
            # Pattern: handle_core_ops(..., []) -> handle_core_ops(..., [{"role": "user", "content": "gibbs C483838 individual Jane Smith"}])
            new_content = re.sub(r'history=\[\]', r'history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}]', content)
            new_content = re.sub(r', \[\]\)', r', [{"role": "user", "content": "gibbs C483838 individual Jane Smith"}])', new_content)
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as file:
                    file.write(new_content)
                print(f"Updated {filepath}")
