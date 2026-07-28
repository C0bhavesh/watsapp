class ShopifyError(Exception):
    """Base for all Shopify-layer failures."""


class ShopifyAuthError(ShopifyError):
    """Token rejected even after a forced refresh."""


class ShopifyThrottled(ShopifyError):
    """GraphQL cost throttle hit — retryable."""


class ShopifyUnavailable(ShopifyError):
    """Network-level failure talking to Shopify — retryable."""


class TokenGrantError(ShopifyError):
    """client_credentials grant failed."""


class ShopifyGraphQLError(ShopifyError):
    def __init__(self, messages: list[str], codes: tuple[str, ...] = ()) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages
        self.codes = codes
