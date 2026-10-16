"""Pull private images with credentials held only in a temporary /run directory."""
import json
import os
import subprocess
import sys
import tempfile

credentials = json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix='quantik-registry-', dir='/run') as directory:
    environment = {**os.environ, 'DOCKER_CONFIG': directory}
    result = subprocess.run(['docker', 'login', '--username', credentials['Username'], '--password-stdin'],
        input=credentials['Secret'], text=True, capture_output=True, env=environment)
    if result.returncode:
        raise SystemExit('Private registry login failed; credential output suppressed')
    print('Temporary registry authentication established', flush=True)
    subprocess.run(['docker', 'compose', '--env-file', '.env.production', 'pull', '--quiet'],
        cwd='/opt/quantik', env=environment, check=True)
print('Images pulled; temporary registry credentials removed')
