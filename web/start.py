"""AMP entrypoint when the repository root is the application directory."""
import os
from pathlib import Path
import sys

import uvicorn
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == '__main__':
    load_dotenv(Path(__file__).resolve().parent / '.env', override=False)
    uvicorn.run('web.app:create_app', factory=True,
                host=os.getenv('WEB_BIND_HOST', '127.0.0.1'),
                port=int(os.getenv('WEB_PORT', '8080')), workers=1,
                access_log=False, proxy_headers=True,
                forwarded_allow_ips=os.getenv('WEB_TRUSTED_PROXIES', '127.0.0.1'))
