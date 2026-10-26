from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

import prized_client as pc


class PrizedProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        # A read (the plugin's sandboxes): it proves the key works and creates nothing. It cannot
        # prove the key may write, so a read-only key passes here and fails on Create sandbox.
        try:
            with pc.PrizedClient(credentials.get("api_key")) as client:
                client.validate()
        except pc.PrizedError as exc:
            raise ToolProviderCredentialValidationError(str(exc)) from None
        except Exception:
            raise ToolProviderCredentialValidationError("Could not check the key with the Prized API.") from None
