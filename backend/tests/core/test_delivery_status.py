from app.core.delivery_status import should_apply_delivery_status


def test_none_current_accepts_any_recognized_status():
    assert should_apply_delivery_status(None, "sent") is True
    assert should_apply_delivery_status(None, "delivered") is True
    assert should_apply_delivery_status(None, "read") is True
    assert should_apply_delivery_status(None, "failed") is True


def test_forward_progression_applies():
    assert should_apply_delivery_status("sent", "delivered") is True
    assert should_apply_delivery_status("delivered", "read") is True
    assert should_apply_delivery_status("sent", "read") is True


def test_out_of_order_regression_rejected():
    assert should_apply_delivery_status("read", "delivered") is False
    assert should_apply_delivery_status("read", "sent") is False
    assert should_apply_delivery_status("delivered", "sent") is False


def test_equal_rank_is_a_noop():
    assert should_apply_delivery_status("delivered", "delivered") is False
    assert should_apply_delivery_status("read", "read") is False


def test_failed_always_applies_going_forward():
    assert should_apply_delivery_status("sent", "failed") is True
    assert should_apply_delivery_status("delivered", "failed") is True
    assert should_apply_delivery_status(None, "failed") is True


def test_failed_is_terminal_nothing_overwrites_it():
    assert should_apply_delivery_status("failed", "sent") is False
    assert should_apply_delivery_status("failed", "delivered") is False
    assert should_apply_delivery_status("failed", "read") is False
    assert should_apply_delivery_status("failed", "failed") is False


def test_unrecognized_new_status_is_rejected():
    assert should_apply_delivery_status("sent", "some_future_status") is False
    assert should_apply_delivery_status(None, "some_future_status") is False
