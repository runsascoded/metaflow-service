import os
import requests
import socket
import time
from services.data.service_configs import max_startup_retries, \
    startup_retry_wait_time_seconds

port = int(os.environ.get("MF_MIGRATION_PORT", 8082))

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
retry_count = max_startup_retries
while retry_count > 0:
    print(retry_count)
    try:
        print(f"Check for running migration service at localhost:{port} ({retry_count} attempts remain):")
        s.connect(('localhost', port))
        print("Migration service reachable!", port)
        break
    except socket.error as e:
        print(f"Migration service not found:")
        print(e)
        print(f"sleeping for {startup_retry_wait_time_seconds}s ...")
        time.sleep(startup_retry_wait_time_seconds)
    finally:
        retry_count = retry_count - 1
# continue
s.close()

url = 'http://localhost:{0}/version'.format(port)
print(f'Getting version from url: {url}')
r = requests.get(url)
text = r.text
print(f'Got version: {text}')
with open('/root/services/migration_service/config', 'w') as conf_file:
    print(text, file=conf_file)
