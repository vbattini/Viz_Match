import asyncio

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from powerbi_dashboard_comparator import browser


class FakeButton:
    def __init__(self, *, visible=False, enabled=True, click_error=None):
        self.visible = visible
        self.enabled = enabled
        self.click_error = click_error
        self.click_kwargs = []

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def wait_for(self, *, state, timeout):
        assert state == "visible"
        assert timeout == browser.CLEAR_SELECTION_VISIBLE_TIMEOUT_MS
        if not self.visible:
            raise PlaywrightTimeoutError("element did not become visible")

    async def click(self, **kwargs):
        self.click_kwargs.append(kwargs)
        if self.click_error is not None:
            raise self.click_error


class FakeButtonList:
    def __init__(self, buttons):
        self._buttons = buttons

    async def count(self):
        return len(self._buttons)

    def nth(self, index):
        return self._buttons[index]


class FakeScope:
    def __init__(self, buttons):
        self.buttons = buttons

    async def count(self):
        return 1

    def get_by_role(self, role, name, exact):
        assert role == "button"
        assert exact is True
        assert name.match("Clear selections")
        assert name.match("Clear selection")
        return FakeButtonList(self.buttons)


class FakeControl:
    def __init__(self, scopes, reveal=()):
        self._scopes = list(scopes)
        self._reveal = list(reveal)
        self.hover_calls = 0

    def locator(self, selector):
        assert selector.startswith("xpath=ancestor::")
        return self._scopes.pop(0)

    async def hover(self, timeout):
        assert timeout == browser.SLICER_CONTROL_ACTION_TIMEOUT_MS
        self.hover_calls += 1
        for button in self._reveal:
            button.visible = True

    async def bounding_box(self):
        return None


FILTER = {"name": "Time Period", "control_type": "dropdown"}


def find_clear(control):
    return asyncio.run(
        browser._clear_selection_button(object(), FILTER, control=control)
    )


def test_visible_clear_button_is_found_without_hovering() -> None:
    button = FakeButton(visible=True)
    control = FakeControl([FakeScope([button]), FakeScope([])])

    assert find_clear(control) is button
    assert control.hover_calls == 0


def test_hidden_clear_button_is_found_after_hovering_slicer() -> None:
    button = FakeButton(visible=False)
    control = FakeControl(
        [FakeScope([button]), FakeScope([])], reveal=[button]
    )

    assert find_clear(control) is button
    assert control.hover_calls == 1


def test_missing_clear_button_returns_none() -> None:
    control = FakeControl([FakeScope([]), FakeScope([])])

    assert find_clear(control) is None
    assert control.hover_calls == 1


def test_clear_button_from_another_slicer_scope_is_not_clicked() -> None:
    other_slicer_button = FakeButton(visible=True)
    control = FakeControl(
        [FakeScope([]), FakeScope([other_slicer_button])]
    )

    assert find_clear(control) is None
    assert control.hover_calls == 1
    assert other_slicer_button.click_kwargs == []


def test_clear_button_still_hidden_after_hover_is_not_returned() -> None:
    button = FakeButton(visible=False)
    control = FakeControl([FakeScope([button]), FakeScope([])])

    assert find_clear(control) is None
    assert control.hover_calls == 1
    assert button.click_kwargs == []


def test_visible_but_disabled_clear_button_is_skipped() -> None:
    button = FakeButton(visible=True, enabled=False)
    control = FakeControl([FakeScope([button]), FakeScope([])])

    assert find_clear(control) is None


def test_clear_click_uses_normal_click_without_force() -> None:
    button = FakeButton(visible=True)

    clicked = asyncio.run(
        browser._click_clear_selection_button(button, "Time Period")
    )

    assert clicked is True
    assert button.click_kwargs == [{"timeout": 5_000, "no_wait_after": True}]


def test_unclickable_clear_button_returns_false_without_force_retry() -> None:
    button = FakeButton(
        visible=True,
        click_error=PlaywrightError("Element is not visible"),
    )

    clicked = asyncio.run(
        browser._click_clear_selection_button(button, "Time Period")
    )

    assert clicked is False
    assert button.click_kwargs == [{"timeout": 5_000, "no_wait_after": True}]
