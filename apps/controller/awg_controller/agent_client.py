import ssl
from urllib.parse import urlsplit
import httpx
from awg_contracts import AgentCapabilities, ActualDeployment, ApplyRequest, TrafficSnapshot, ValidationResult


class AgentError(RuntimeError):
    pass


class AgentClient:
    def __init__(self, settings):
        context = ssl.create_default_context(cafile=settings.agent_ca)
        context.load_cert_chain(settings.controller_cert, settings.controller_key)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.http = httpx.AsyncClient(verify=context, timeout=30, trust_env=False, follow_redirects=False)

    async def close(self):
        await self.http.aclose()

    async def request(self, registration, method, path, model, body=None):
        parsed = urlsplit(registration.management_url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise AgentError('invalid management origin')
        try:
            response = await self.http.request(method, registration.management_url.rstrip('/')+path,
                json=body.model_dump(mode='json') if body else None,
                extensions={'sni_hostname': f'{registration.node_id}.agents.awg.internal'})
            if response.status_code == 404 and path.endswith('/state'):
                from uuid import UUID
                return ActualDeployment(deployment_id=UUID(path.split('/')[-2]))
            response.raise_for_status()
            return model.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            raise AgentError('agent request failed') from None

    async def capabilities(self, registration):
        value = await self.request(registration, 'GET', '/v1/capabilities', AgentCapabilities)
        if value.node_id != registration.node_id:
            raise AgentError('agent identity mismatch')
        return value

    async def validate(self, registration, desired):
        return await self.request(registration,'POST',f'/v1/deployments/{desired.deployment_id}/validate',ValidationResult,desired)

    async def state(self, registration, deployment_id):
        value = await self.request(registration,'GET',f'/v1/deployments/{deployment_id}/state',ActualDeployment)
        if str(value.deployment_id) != str(deployment_id):
            raise AgentError('deployment identity mismatch')
        return value

    async def apply(self, registration, desired, expected):
        value = await self.request(registration,'PUT',f'/v1/deployments/{desired.deployment_id}/apply',ActualDeployment,
            ApplyRequest(desired=desired,expected_applied_revision=expected))
        if value.deployment_id != desired.deployment_id:
            raise AgentError('deployment identity mismatch')
        return value

    async def traffic(self, registration, deployment_id):
        value = await self.request(registration,'GET',f'/v1/deployments/{deployment_id}/traffic',TrafficSnapshot)
        if value.node_id != registration.node_id or str(value.deployment_id) != str(deployment_id):
            raise AgentError('traffic identity mismatch')
        return value
