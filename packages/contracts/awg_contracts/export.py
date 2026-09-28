"""Run inside tooling container: python -m awg_contracts.export."""
import json
from pathlib import Path

from .models import ALL_MODELS


def export(destination: Path | None = None):
    directory = destination or Path(__file__).resolve().parent.parent / 'v1'
    directory.mkdir(parents=True, exist_ok=True)
    components = {}
    for model in ALL_MODELS:
        schema = model.model_json_schema()
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        (directory / f'{model.__name__}.schema.json').write_text(json.dumps(schema, indent=2) + '\n')
        openapi_schema = model.model_json_schema(ref_template='#/components/schemas/{model}')
        components.update(openapi_schema.pop('$defs', {}))
        components[model.__name__] = openapi_schema
    paths = {}
    operations = [
        ('/v1/capabilities', 'get', None, 'AgentCapabilities'),
        ('/v1/deployments/{deployment_id}/validate', 'post', 'DesiredDeployment', 'ValidationResult'),
        ('/v1/deployments/{deployment_id}/apply', 'put', 'ApplyRequest', 'ActualDeployment'),
        ('/v1/deployments/{deployment_id}/state', 'get', None, 'ActualDeployment'),
        ('/v1/deployments/{deployment_id}/traffic', 'get', None, 'TrafficSnapshot'),
        ('/v1/internal/subscriptions/material', 'post', 'SubscriptionRequest', 'SubscriptionMaterial'),
    ]
    for path, method, request, response in operations:
        ref = lambda name: {'$ref': f'#/components/schemas/{name}'}
        operation = {'operationId': method + '_' + response,
                     'security': [{'serviceToken': []}] if 'internal' in path else [{'mutualTLS': []}],
                     'responses': {'200': {'description': 'Success', 'content': {'application/json': {'schema': ref(response)}}},
                                   'default': {'description': 'Sanitized error', 'content': {'application/json': {'schema': ref('APIError')}}}}}
        if '{deployment_id}' in path:
            operation['parameters'] = [{'name': 'deployment_id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}}]
        if request:
            operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': ref(request)}}}
        paths.setdefault(path, {})[method] = operation
    document = {'openapi': '3.1.0', 'info': {'title': 'Remnawave AWG internal contracts', 'version': '1.0.0'},
                'paths': paths, 'components': {'schemas': components,
                'securitySchemes': {'mutualTLS': {'type': 'mutualTLS'},
                                    'serviceToken': {'type': 'http', 'scheme': 'bearer'}}}}
    (directory / 'openapi.json').write_text(json.dumps(document, indent=2) + '\n')


if __name__ == '__main__':
    export()
