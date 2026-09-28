"""Internal-only lab proxy implementing upstream's required forwarded headers.

Never published on the host. Production must use a real TLS reverse proxy.
"""
import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

app = FastAPI()
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}

@app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
async def proxy(path: str, request: Request):
    headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP and not key.lower().startswith('x-forwarded-')}
    headers['x-forwarded-proto'] = 'https'
    headers['x-forwarded-for'] = request.client.host
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        response = await client.request(request.method, 'http://remnawave:3000/' + path, params=request.query_params, content=await request.body(), headers=headers)
    return Response(response.content, status_code=response.status_code, headers={k:v for k,v in response.headers.items() if k.lower() not in HOP | {'content-encoding'}})

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8080, access_log=False)
