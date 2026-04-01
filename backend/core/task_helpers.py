import re
import os

def parse_script_meta(filepath, default_name):
    name = default_name
    cron = None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(8192)

            name_match = re.search(r'任务名称[：:]\s*([^\r\n*]+)', content, re.IGNORECASE)
            if name_match:
                name = name_match.group(1).strip()

            if name == default_name:
                name_match = re.search(r'\b(?:const|let|var)\s+jsname\s*=\s*[\'"](.+?)[\'"]', content)
                if name_match:
                    name = name_match.group(1).strip()

            if name == default_name:
                name_match = re.search(r'\b(?:new\s+)?Env\s*\(\s*[\'"](.+?)[\'"]\s*\)', content)
                if name_match:
                    name = name_match.group(1).strip()
            
            cron_match = re.search(r'(?:cron|@cron)[^\w\d]+([0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+(?:\s+[0-9\*/,-]+)?)', content, re.IGNORECASE)
            if cron_match:
                raw_cron = cron_match.group(1).strip()
                raw_cron = re.sub(r'\s*\*\s*/\s*$', '', raw_cron)
                cron = raw_cron.strip()
    except:
        pass
    return name, cron