import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('awg_controller.app:app',host='0.0.0.0',port=int(os.environ.get('AWG_PORT','8080')),access_log=False,proxy_headers=False)
