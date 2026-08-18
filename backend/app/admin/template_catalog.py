"""Registry of the store's approved WhatsApp templates, for the admin manual-resend dialog.

Adding a template Meta approves in the future is a ONE-ENTRY addition to TEMPLATE_CATALOG below
-- no frontend change and no send_template change is needed, since both are already generic over
this registry (see docs/superpowers/specs/2026-08-19-admin-emoji-template-picker-design.md).

Mirrors the exact template shapes app/channels/shopify_webhook.py already sends automatically:
cod_confirmation/prepaid_order use NAMED body params, order_shipped/order_delivered use
POSITIONAL body params (see send_template's body_params: Mapping[str, str] | Sequence[str]).
"""

from dataclasses import dataclass

from app.channels.shopify_orders import split_variant_options
from app.shopify.models import Order


@dataclass(frozen=True)
class TemplateField:
    key: str
    label: str
    default_from: str  # key into the dict resolve_template_defaults() returns; "" = no default


@dataclass(frozen=True)
class TemplateDef:
    label: str
    language: str
    param_style: str  # "named" or "positional"
    fields: tuple[TemplateField, ...]
    has_confirm_cancel_buttons: bool = False


_CONFIRMATION_FIELDS = (
    TemplateField("customer_name", "Customer name", "customer_name"),
    TemplateField("order_id", "Order ID", "order_name"),
    TemplateField("product_name", "Product name", "product_name"),
    TemplateField("product_color", "Color", "product_color"),
    TemplateField("product_size", "Size", "product_size"),
    TemplateField("product_amount", "Amount", "product_amount"),
)

TEMPLATE_CATALOG: dict[str, TemplateDef] = {
    "cod_confirmation": TemplateDef(
        label="COD Confirmation", language="en", param_style="named",
        fields=_CONFIRMATION_FIELDS,
        has_confirm_cancel_buttons=True,
    ),
    "prepaid_order": TemplateDef(
        label="Prepaid Order Confirmation", language="en", param_style="named",
        fields=_CONFIRMATION_FIELDS,
    ),
    "order_shipped": TemplateDef(
        label="Shipped Notice", language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
            TemplateField("tracking_company", "Courier", "tracking_company"),
            TemplateField("tracking_link", "Tracking link/number", "tracking_link"),
        ),
    ),
    "order_delivered": TemplateDef(
        label="Delivered Notice", language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
        ),
    ),
}


def resolve_template_defaults(order: Order) -> dict[str, str]:
    """Derive every TemplateField.default_from key any catalog entry might reference, from ONE
    order. Always returns every key with a string value ("" when nothing is available) so a
    caller never needs a presence check -- this mirrors shopify_webhook.py's own EMPTY_PARAM_
    PLACEHOLDER-style "never leave a template param structurally missing" posture, except here an
    empty string (not the placeholder) is correct: the admin sees a blank, editable field, not a
    literal "-" they'd have to notice and clear.
    """
    first_item = order.line_items[0] if order.line_items else None
    product_color, product_size = (
        split_variant_options(first_item.variant_title) if first_item else (None, None)
    )
    first_fulfillment = order.fulfillments[0] if order.fulfillments else None
    tracking_link = ""
    tracking_company = ""
    if first_fulfillment is not None:
        tracking_company = first_fulfillment.tracking_company or ""
        tracking_link = first_fulfillment.tracking_url or first_fulfillment.tracking_number or ""
    customer_name = ""
    if order.customer is not None:
        customer_name = " ".join(
            p for p in (order.customer.first_name, order.customer.last_name) if p
        ).strip()
    return {
        "customer_name": customer_name,
        "order_name": order.name,
        "product_name": (first_item.title if first_item else "") or "",
        "product_color": product_color or "",
        "product_size": product_size or "",
        "product_amount": (
            (first_item.price.amount if first_item and first_item.price else "") or ""
        ),
        "tracking_company": tracking_company,
        "tracking_link": tracking_link,
    }
