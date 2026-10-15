"""Maps connector type keys to plugin classes. Plugins import their platform SDKs lazily."""
from __future__ import annotations

from .base import PullConnector
from .plugins.aws_bedrock import AwsBedrockConnector
from .plugins.azure_foundry import AzureFoundryConnector
from .plugins.azure_platform import AzurePlatformConnector
from .plugins.demo_platform import DemoPlatformConnector
from .plugins.salesforce import SalesforceConnector
from .plugins.servicenow import ServiceNowConnector

PLUGINS: dict[str, type[PullConnector]] = {
    cls.type_key: cls for cls in (DemoPlatformConnector, AzurePlatformConnector, AzureFoundryConnector,
                                  AwsBedrockConnector, ServiceNowConnector, SalesforceConnector)
}


def plugin_for(type_key: str) -> PullConnector | None:
    cls = PLUGINS.get(type_key)
    return cls() if cls else None
