"""Load disposable stock token without exposing it in Compose config or argv."""
import os
from pathlib import Path
import runpy
os.environ['REMNAWAVE_API_TOKEN'] = Path('/lab-credentials/stock-token').read_text().strip()
runpy.run_module('awg_controller', run_name='__main__')
