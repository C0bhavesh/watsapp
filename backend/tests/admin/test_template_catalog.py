from app.admin.template_catalog import TEMPLATE_CATALOG, resolve_template_defaults
from app.channels.shopify_orders import split_variant_options
from app.shopify.models import LineItem, Money, Order


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = dict(
        gid="gid://shopify/Order/1",
        name="tavas4142",
        email=None,
        phone=None,
        shipping_phone="+919664290413",
        billing_phone=None,
        financial_status="pending",
        fulfillment_status=None,
        cancelled_at=None,
        tags=(),
        payment_gateway_names=("Cash on Delivery (COD)",),
        total=Money(amount="999.00", currency="INR"),
        customer_locale="en",
        line_items=(
            LineItem(
                title="Black Premium Cotton Co-Ord Set",
                quantity=1,
                variant_title="Black / XL",
                price=Money(amount="899.00", currency="INR"),
            ),
        ),
        customer=None,
        fulfillments=(),
    )
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


def test_catalog_has_all_four_known_templates() -> None:
    assert set(TEMPLATE_CATALOG) == {
        "cod_confirmation", "prepaid_order", "order_shipped", "order_delivered",
    }


def test_cod_confirmation_has_confirm_cancel_buttons() -> None:
    assert TEMPLATE_CATALOG["cod_confirmation"].has_confirm_cancel_buttons is True


def test_prepaid_order_has_no_buttons() -> None:
    assert TEMPLATE_CATALOG["prepaid_order"].has_confirm_cancel_buttons is False


def test_shipped_and_delivered_have_no_buttons() -> None:
    assert TEMPLATE_CATALOG["order_shipped"].has_confirm_cancel_buttons is False
    assert TEMPLATE_CATALOG["order_delivered"].has_confirm_cancel_buttons is False


def test_split_variant_options_is_public_and_correct() -> None:
    assert split_variant_options("Black / XL") == ("Black", "XL")
    assert split_variant_options(None) == (None, None)


def test_resolve_template_defaults_derives_product_fields_from_first_line_item() -> None:
    defaults = resolve_template_defaults(_order())
    assert defaults["order_name"] == "tavas4142"
    assert defaults["product_name"] == "Black Premium Cotton Co-Ord Set"
    assert defaults["product_color"] == "Black"
    assert defaults["product_size"] == "XL"
    assert defaults["product_amount"] == "899.00"


def test_resolve_template_defaults_tracking_link_prefers_url_over_number() -> None:
    order = _order(
        fulfillments=(
            __import__("app.shopify.models", fromlist=["Fulfillment"]).Fulfillment(
                gid="gid://shopify/Fulfillment/1",
                status="success",
                tracking_company="Delhivery",
                tracking_number="AB123",
                tracking_url="https://track.example/AB123",
            ),
        ),
    )
    defaults = resolve_template_defaults(order)
    assert defaults["tracking_company"] == "Delhivery"
    assert defaults["tracking_link"] == "https://track.example/AB123"


def test_resolve_template_defaults_tracking_link_falls_back_to_number() -> None:
    order = _order(
        fulfillments=(
            __import__("app.shopify.models", fromlist=["Fulfillment"]).Fulfillment(
                gid="gid://shopify/Fulfillment/1",
                status="success",
                tracking_company="Delhivery",
                tracking_number="AB123",
                tracking_url=None,
            ),
        ),
    )
    assert resolve_template_defaults(order)["tracking_link"] == "AB123"


def test_resolve_template_defaults_blank_when_no_line_items_or_fulfillments() -> None:
    order = _order(line_items=(), fulfillments=())
    defaults = resolve_template_defaults(order)
    assert defaults["product_name"] == ""
    assert defaults["tracking_company"] == ""
    assert defaults["tracking_link"] == ""
