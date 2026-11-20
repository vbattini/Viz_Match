import asyncio
import json
import random
from pathlib import Path
from types import SimpleNamespace

from Backend import agents
from Backend import browser
from Backend import validation_scenarios as vs


def _filter(name, values, selected=None, control_type="checkbox"):
    return {
        "name": name,
        "values": values,
        "selected": selected if selected is not None else [],
        "control_type": control_type,
    }


def test_normalize_label_casefolds_and_collapses_whitespace():
    assert vs.normalize_label("  Region \t North ") == "region north"


def test_aggregate_labels_are_excluded_from_individual_options():
    item = _filter("Region", ["All", "Select All", "(All)", "North", "South", "North"])
    assert vs.individual_options(item) == ["North", "South"]


def test_common_filters_match_by_normalized_name_and_keep_shared_pairs_only():
    a = [_filter("Region ", ["North", "South"]), _filter("Only A", ["x"])]
    b = [_filter("REGION", ["north", "East"])]
    common = vs.common_filters(a, b)
    assert [entry["key"] for entry in common] == ["region"]
    assert common[0]["pairs"] == [("North", "north")]


def test_single_plan_is_seeded_and_not_always_first():
    common = vs.common_filters(
        [_filter("Region", ["North", "South", "East", "West"]),
         _filter("Year", ["2022", "2023", "2024"], control_type="radio")],
        [_filter("Region", ["North", "South", "East", "West"]),
         _filter("Year", ["2022", "2023", "2024"], control_type="radio")],
    )
    picked_values = set()
    for seed in range(40):
        plan = vs.plan_single_filter(common, random.Random(seed))
        assert plan is not None
        assert plan["value_a"] in {"North", "South", "East", "West", "2022", "2023", "2024"}
        picked_values.add(plan["value_a"])
    assert len(picked_values) > 1
    assert vs.plan_single_filter(common, random.Random(7)) == vs.plan_single_filter(
        common, random.Random(7)
    )


def test_single_plan_returns_none_without_shared_options():
    common = vs.common_filters([_filter("Region", ["North"])], [_filter("Region", ["South"])])
    assert vs.plan_single_filter(common, random.Random(1)) is None


def test_multi_plan_uses_a_distinct_filter_and_one_unselected_shared_value():
    common = vs.common_filters(
        [
            _filter("Region", ["North", "South"], selected=["North"]),
            _filter("Year", ["2022", "2023", "2024"], selected=["2022"], control_type="radio"),
        ],
        [
            _filter("Region", ["North", "South"], selected=["North"]),
            _filter("Year", ["2022", "2023", "2024"], selected=["2022"], control_type="radio"),
        ],
    )
    plan = vs.plan_multi_filter(
        common, random.Random(3), excluded_filter_key="region"
    )
    assert plan is not None
    assert plan["name_a"] == "Year"
    assert len(plan["values_a"]) == 1
    assert len(plan["values_b"]) == 1
    assert plan["values_a"][0] in {"2023", "2024"}
    assert plan["values_a"] == plan["values_b"]


def test_multi_plan_skips_when_only_the_first_filter_is_available():
    common = vs.common_filters(
        [_filter("Facility", ["17 - Menominee", "27 - Pensacola"], control_type="checkbox_dropdown")],
        [_filter("Facility", ["17 - Menominee", "27 - Pensacola"], control_type="checkbox_dropdown")],
    )
    assert vs.plan_multi_filter(
        common, random.Random(5), excluded_filter_key="facility"
    ) is None


def test_empty_baseline_matches_displayed_all_value():
    from Backend import browser

    item = _filter("Facility", ["17 - Menominee", "27 - Pensacola"], control_type="checkbox_dropdown")
    assert browser._filter_values_equivalent(item, [], ["All"])
    assert browser._filter_values_equivalent(item, [], [])
    assert not browser._filter_values_equivalent(item, [], ["17 - Menominee"])


def test_unreadable_or_failed_selection_is_unverified_not_fail():
    assert vs.selection_outcome(None, True) == vs.STATUS_UNVERIFIED
    assert vs.selection_outcome(False, True) == vs.STATUS_FAIL
    assert vs.selection_outcome(True, True) == vs.STATUS_PASS


def test_multi_plan_accepts_radio_and_dropdown_filter_types():
    common = vs.common_filters(
        [
            _filter("Region", ["North", "South"]),
            _filter("Mode", ["A", "B"], control_type="radio"),
        ],
        [
            _filter("Region", ["North", "South"]),
            _filter("Mode", ["A", "B"], control_type="radio"),
        ],
    )
    plan = vs.plan_multi_filter(
        common, random.Random(1), excluded_filter_key="region"
    )
    assert plan is not None
    assert plan["name_a"] == "Mode"
    assert len(plan["values_a"]) == 1


def test_scenario_codes_include_merged_table_and_hierarchy_scenario():
    assert tuple(vs.SCENARIO_BY_CODE) == ("01", "02", "03")
    assert vs.SCENARIO_TABLE_HEADERS in vs.SCENARIO_DIRECTORIES


def test_selection_outcome_never_reports_pass_for_unverified_state():
    assert vs.selection_outcome(True, True) == vs.STATUS_PASS
    assert vs.selection_outcome(True, False) == vs.STATUS_FAIL
    assert vs.selection_outcome(True, None) == vs.STATUS_UNVERIFIED


def test_scenario_status_skipped_is_never_pass():
    assert vs.scenario_status([vs.STATUS_SKIPPED]) == vs.STATUS_SKIPPED
    assert vs.scenario_status([vs.STATUS_PASS, vs.STATUS_SKIPPED]) == vs.STATUS_PASS
    assert vs.scenario_status([vs.STATUS_PASS, vs.STATUS_UNVERIFIED]) == vs.STATUS_UNVERIFIED
    assert vs.scenario_status([vs.STATUS_UNVERIFIED, vs.STATUS_FAIL]) == vs.STATUS_FAIL


def test_pair_visuals_by_normalized_title_and_type():
    a = [{"title": "Sales by Region", "visual_type": "table"},
         {"title": "Only A", "visual_type": "matrix"}]
    b = [{"title": "sales  by region", "visual_type": "table"},
         {"title": "Only B", "visual_type": "matrix"}]
    pairs = vs.pair_visuals(a, b)
    assert pairs[0][1] is not None and pairs[0][1]["title"] == "sales  by region"
    assert pairs[1][1] is None
    assert pairs[2][0] is None and pairs[2][1]["title"] == "Only B"


def test_pair_visuals_rejects_incompatible_types():
    pairs = vs.pair_visuals(
        [{"title": "Sales", "visual_type": "table"}],
        [{"title": "Sales", "visual_type": "barChart"}],
    )
    assert pairs[0][1] is None


def test_drill_step_requires_changed_rows_and_drill_up():
    before = {"rows": ["a"], "headers": [], "drill_up_available": False}
    assert vs.drill_step_verified(before, {"rows": ["a", "b"], "headers": [], "drill_up_available": True})
    assert not vs.drill_step_verified(before, {"rows": ["a"], "headers": [], "drill_up_available": True})
    assert vs.drill_step_verified(before, {"rows": ["a"], "headers": ["Department"], "drill_up_available": False})
    assert not vs.drill_step_verified(before, {"rows": ["a"], "headers": [], "drill_up_available": False})
    assert not vs.drill_step_verified(before, None)


def test_drill_execution_status_requires_verified_visuals_and_restoration():
    assert vs._drill_execution_status({
        "visuals": [{"status": vs.STATUS_PASS, "restored": True}]
    }) == vs.STATUS_PASS
    assert vs._drill_execution_status({
        "visuals": [{"status": vs.STATUS_PASS, "restored": False}]
    }) == vs.STATUS_UNVERIFIED
    assert vs._drill_execution_status({
        "visuals": [{"status": vs.STATUS_UNVERIFIED, "restored": True}]
    }) == vs.STATUS_UNVERIFIED
    assert vs._drill_execution_status({"status": vs.STATUS_SKIPPED}) == vs.STATUS_SKIPPED


def test_drill_step_does_not_capture_duplicate_initial_screenshot(monkeypatch, tmp_path):
    async def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("drilldown must not capture a duplicate initial image")

    monkeypatch.setattr(vs, "_capture_side", unexpected_capture)

    result = asyncio.run(
        vs._drill_step(
            SimpleNamespace(label="dashboard_A"),
            [],
            tmp_path,
            0,
        )
    )

    assert result == {"visuals": []}


def test_safe_path_part_removes_unsafe_characters():
    assert "/" not in vs.safe_path_part("Sales: 2024/Q1")
    assert vs.safe_path_part("///") == "item"
    assert "/" not in vs.safe_path_part("a/b\\c")


def test_inventory_failure_is_recorded_and_not_raised(monkeypatch, tmp_path):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("Power BI dropdown did not open")

    monkeypatch.setattr(vs, "_accessible_inventory", boom)
    class FakePage:
        async def wait_for_timeout(self, _milliseconds):
            return None

    side = vs._Side("dashboard_A", "https://example", "Page", FakePage(), tmp_path)

    filters = asyncio.run(side.inventory())

    assert filters == []
    assert "dropdown did not open" in side.inventory_error


def test_restore_error_is_recorded_and_not_raised(tmp_path):
    class _Broken:
        async def restore(self):
            raise RuntimeError("Cannot read current selections for filter 'Rank'")

    restored, error = asyncio.run(vs._safe_restore(_Broken()))

    assert restored is False
    assert "Cannot read current selections" in error


def test_single_step_does_not_capture_unverified_selection(monkeypatch, tmp_path):
    class _Side:
        label = "dashboard_A"

        async def restore(self):
            return True

    async def unreadable_selection(*_args):
        return {"filter": "Region", "requested": ["North"], "applied": True, "verified": None}

    async def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("an unverified selection must not be captured")

    monkeypatch.setattr(vs, "_apply_single", unreadable_selection)
    monkeypatch.setattr(vs, "_capture_side", unexpected_capture)
    plan = {"name_a": "Region", "value_a": "North"}

    result = asyncio.run(
        vs._single_step(_Side(), plan, tmp_path, 0, keep_state=True)
    )

    assert result["verified"] is None
    assert result["screenshot"] is None
    assert result["kept_for_next"] is False
    assert result["restored"] is True


def _filter_transition(
    monkeypatch,
    current_values,
    initial_vendor=("All",),
    selected=("North",),
):
    vendor = _filter(
        "Vendor",
        ["All", "North", "South"],
        selected=list(initial_vendor) if initial_vendor is not None else None,
    )
    if initial_vendor is None:
        vendor["selected"] = None

    class _Side:
        page = object()
        baseline = [
            vendor,
            _filter("Rank", ["A", "B"], selected=["A"]),
        ]

    async def read_current(_page, item):
        return current_values.get(item["name"])

    monkeypatch.setattr(vs, "_current_filter_value", read_current)
    return asyncio.run(
        vs._verify_filter_transition(_Side(), "Vendor", list(selected))
    )


def test_single_filter_transition_requires_target_value_to_change(monkeypatch):
    transition = _filter_transition(
        monkeypatch, {"Rank": ["A"]}, selected=("All",)
    )

    assert transition["changed_from_initial"] is False
    assert transition["other_filters_unchanged"] is True
    assert transition["verified"] is False


def test_single_filter_transition_does_not_rescan_other_filters(monkeypatch):
    transition = _filter_transition(
        monkeypatch, {"Rank": ["B"]}
    )

    assert transition["changed_from_initial"] is True
    assert transition["other_filters_unchanged"] is True
    assert transition["other_filters"] == []
    assert transition["verified"] is True


def test_single_filter_transition_is_unverified_when_initial_values_are_unreadable(monkeypatch):
    transition = _filter_transition(
        monkeypatch, {"Rank": ["A"]}, initial_vendor=None
    )

    assert transition["changed_from_initial"] is None
    assert transition["other_filters_unchanged"] is True
    assert transition["verified"] is None


def test_single_step_does_not_capture_when_filter_transition_fails(monkeypatch, tmp_path):
    class _Side:
        label = "dashboard_A"

        async def restore(self):
            return True

    async def applied_selection(*_args):
        return {
            "filter": "Vendor",
            "requested": ["North"],
            "selected": ["North"],
            "applied": True,
            "verified": True,
        }

    async def unchanged_filter(*_args):
        return {"changed_from_initial": False, "verified": False}

    async def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("an unverified transition must not be captured")

    monkeypatch.setattr(vs, "_apply_single", applied_selection)
    monkeypatch.setattr(vs, "_verify_filter_transition", unchanged_filter)
    monkeypatch.setattr(vs, "_capture_side", unexpected_capture)

    result = asyncio.run(
        vs._single_step(
            _Side(), {"name_a": "Vendor", "value_a": "North"}, tmp_path, 0
        )
    )

    assert result["verified"] is False
    assert result["screenshot"] is None


def test_chained_multi_step_does_not_capture_when_base_readback_is_unavailable(monkeypatch, tmp_path):
    applied = []
    screenshots = []

    class _Side:
        label = "dashboard_A"
        page = object()
        filters = [{"name": "Facility", "control_type": "checkbox_dropdown"},
                   {"name": "Rank", "control_type": "checkbox_dropdown"}]

        async def restore(self):
            return True

    async def fake_apply(side, name, value):
        applied.append((name, value))
        return {"filter": name, "requested": [value], "applied": True, "selected": [value], "verified": True}

    async def no_readback(page, item):
        return None

    async def fake_shot(side, folder, settle, **_kwargs):
        screenshots.append("shot.png")
        return "shot.png"

    monkeypatch.setattr(vs, "_apply_single", fake_apply)
    monkeypatch.setattr(vs, "_current_filter_value", no_readback)
    monkeypatch.setattr(vs, "_capture_side", fake_shot)
    base = {"record": {"filter": "Facility", "requested": ["17 - Menominee"], "verified": True}}
    plan = {"name_a": "Rank", "values_a": ["C"], "name_b": "Rank", "values_b": ["C"]}

    result = asyncio.run(vs._multi_step(_Side(), plan, tmp_path, 0, base=base))

    assert applied == [("Rank", "C")]
    assert result["record"]["base_selection"]["verified"] is None
    assert result["record"]["base_selection"]["source"] == "dom_readback_unavailable"
    assert result["record"]["screenshot_skipped_reason"]
    assert result["verified"] is None
    assert result["screenshot"] is None
    assert screenshots == []
    assert result["restored"] is True


def test_chained_multi_step_captures_only_after_preserving_base_selection(monkeypatch, tmp_path):
    applied = []
    captured = []

    class _Side:
        label = "dashboard_A"
        page = object()
        filters = [{"name": "Region"}, {"name": "Year"}]

        async def restore(self):
            return True

    async def fake_apply(side, name, value):
        applied.append((name, value))
        return {
            "filter": name,
            "requested": [value],
            "applied": True,
            "selected": [value],
            "verified": True,
        }

    async def read_base(_page, item):
        assert item["name"] == "Region"
        return ["North"]

    async def fake_shot(_side, _folder, _settle, **kwargs):
        captured.append(kwargs["filter_selections"])
        return "shot.png"

    monkeypatch.setattr(vs, "_apply_single", fake_apply)
    monkeypatch.setattr(vs, "_current_filter_value", read_base)
    monkeypatch.setattr(vs, "_capture_side", fake_shot)
    base = {
        "record": {
            "filter": "Region",
            "requested": ["North"],
            "selected": ["North"],
            "verified": True,
        }
    }
    plan = {"name_a": "Year", "values_a": ["2024"], "name_b": "Year", "values_b": ["2024"]}

    result = asyncio.run(vs._multi_step(_Side(), plan, tmp_path, 0, base=base))

    assert applied == [("Year", "2024")]
    assert result["record"]["base_selection"]["verified"] is True
    assert result["verified"] is True
    assert result["screenshot"] == "shot.png"
    assert [item["filter_name"] for item in captured[0]] == ["Region", "Year"]
    assert result["restored"] is True


def test_chained_multi_step_does_not_capture_if_first_selection_changed(monkeypatch, tmp_path):
    screenshots = []

    class _Side:
        label = "dashboard_A"
        page = object()
        filters = [{"name": "Region"}, {"name": "Year"}]

        async def restore(self):
            return True

    async def fake_apply(_side, name, value):
        return {
            "filter": name,
            "requested": [value],
            "applied": True,
            "selected": [value],
            "verified": True,
        }

    async def changed_base(_page, _item):
        return ["South"]

    async def fake_shot(*_args, **_kwargs):
        screenshots.append("shot.png")
        return "shot.png"

    monkeypatch.setattr(vs, "_apply_single", fake_apply)
    monkeypatch.setattr(vs, "_current_filter_value", changed_base)
    monkeypatch.setattr(vs, "_capture_side", fake_shot)
    base = {
        "record": {
            "filter": "Region",
            "requested": ["North"],
            "selected": ["North"],
            "verified": True,
        }
    }
    plan = {"name_a": "Year", "values_a": ["2024"], "name_b": "Year", "values_b": ["2024"]}

    result = asyncio.run(vs._multi_step(_Side(), plan, tmp_path, 0, base=base))

    assert result["record"]["base_selection"]["verified"] is False
    assert result["verified"] is False
    assert result["screenshot"] is None
    assert screenshots == []


def test_filter_scenarios_run_each_scenario_with_one_filter(
    monkeypatch, tmp_path
):
    calls = []

    class _Side:
        def __init__(self, label):
            self.label = label

    async def first_step(side, _plan, _folder, _settle, *, keep_state):
        calls.append(("first", side.label))
        return {
            "verified": True,
            "kept_for_next": keep_state,
            "record": {"filter": "Region", "requested": ["North"], "verified": True},
        }

    monkeypatch.setattr(vs, "_single_step", first_step)
    sides = (_Side("dashboard_A"), _Side("dashboard_B"))
    dirs = {
        vs.SCENARIO_SINGLE_FILTER: tmp_path / "single",
        vs.SCENARIO_MULTIPLE_FILTER: tmp_path / "multiple",
    }

    records = asyncio.run(
        vs._run_filter_scenarios(
            sides,
            [vs.SCENARIO_SINGLE_FILTER, vs.SCENARIO_MULTIPLE_FILTER],
            None,
            {"name_a": "Region", "name_b": "Region"},
            {"name_a": "Year", "name_b": "Year"},
            dirs,
            0,
        )
    )

    assert [step for step, _side in calls] == ["first", "first", "first", "first"]
    assert all(
        records[side.label][vs.SCENARIO_SINGLE_FILTER]["verified"] is True
        for side in sides
    )


def test_filter_scenarios_do_not_chain_filter_selections(
    monkeypatch, tmp_path
):
    second_step_calls = []

    class _Side:
        def __init__(self, label):
            self.label = label

    async def first_step(side, _plan, _folder, _settle, *, keep_state):
        verified = side.label == "dashboard_A"
        return {
            "verified": verified,
            "kept_for_next": keep_state and verified,
            "record": {"filter": "Region", "requested": ["North"], "verified": verified},
            "restored": not (keep_state and verified),
        }

    monkeypatch.setattr(vs, "_single_step", first_step)
    sides = (_Side("dashboard_A"), _Side("dashboard_B"))
    dirs = {
        vs.SCENARIO_SINGLE_FILTER: tmp_path / "single",
        vs.SCENARIO_MULTIPLE_FILTER: tmp_path / "multiple",
    }

    records = asyncio.run(
        vs._run_filter_scenarios(
            sides,
            [vs.SCENARIO_SINGLE_FILTER, vs.SCENARIO_MULTIPLE_FILTER],
            None,
            {"name_a": "Region", "name_b": "Region"},
            {"name_a": "Year", "name_b": "Year"},
            dirs,
            0,
        )
    )

    assert second_step_calls == []
    assert all(
        records[side.label][vs.SCENARIO_MULTIPLE_FILTER]["verified"]
        == (side.label == "dashboard_A")
        for side in sides
    )


def test_drill_clicks_until_next_level_button_is_disabled(monkeypatch, tmp_path):
    buttons = [
        [{"key": "Table", "title": "Table", "visual_type": "tableex", "enabled": True, "label": "Go to the next level in the hierarchy", "action": "drill_down", "tag": "t1"}],
        [{"key": "Table", "title": "Table", "visual_type": "tableex", "enabled": True, "label": "Go to the next level in the hierarchy", "action": "drill_down", "tag": "t2"}],
        [{"key": "Table", "title": "Table", "visual_type": "tableex", "enabled": False, "label": "Go to the next level in the hierarchy", "action": "drill_down", "tag": ""}],
    ]
    clicked = []
    snapshots = iter(range(100))

    class _Locator:
        def __init__(self, selector):
            self.selector = selector

        async def click(self):
            clicked.append(self.selector)

    class _Mouse:
        async def move(self, x, y):
            return None

    class _Page:
        mouse = _Mouse()

        async def evaluate(self, script, arg=None):
            if script is vs._VISUAL_CENTERS_SCRIPT:
                return [{"key": "Table", "title": "Table", "visual_type": "tableex", "x": 1, "y": 1}]
            if script is vs._DRILL_BUTTONS_SCRIPT:
                return buttons.pop(0)
            raise AssertionError("unexpected script")

        async def wait_for_timeout(self, ms):
            return None

        def locator(self, selector):
            return _Locator(selector)

    async def fake_snapshot(side, title):
        return {"rows": [f"row-{next(snapshots)}"], "headers": [], "drill_up_available": True}

    captures = []

    async def fake_capture(side, scenario_id, descriptor, settle, **kwargs):
        captures.append((scenario_id, descriptor, kwargs))
        return f"{scenario_id}_{descriptor}.png"

    monkeypatch.setattr(vs, "_snapshot_visual", fake_snapshot)
    monkeypatch.setattr(vs, "_capture_scenario_png", fake_capture)
    monkeypatch.setattr(vs, "drill_step_verified", lambda before, after: True)
    side = vs._Side("dashboard_A", "https://example", "Page", _Page(), tmp_path)

    result = asyncio.run(vs._drill_visual(side, "Table", tmp_path, 0))

    assert result["status"] == vs.STATUS_PASS
    assert result["stop_reason"] == "next-level control disabled"
    assert [step["level"] for step in result["steps"]] == [1, 2]
    assert all(step["before_screenshot"] and step["after_screenshot"] for step in result["steps"])
    assert result["initial_screenshot"] == result["steps"][0]["before_screenshot"]
    assert clicked == ['[data-validation-drill="t1"]', '[data-validation-drill="t2"]']
    assert [capture[0] for capture in captures] == ["04"] * 4
    assert [capture[2]["drill_down_level"] for capture in captures] == [0, 1, 1, 2]


def test_drill_up_and_expand_controls_are_not_used_as_drill_down():
    class _Page:
        async def evaluate(self, script, prefix):
            assert script is vs._DRILL_BUTTONS_SCRIPT
            assert prefix == "drill1"
            return [
                {
                    "key": "Table",
                    "action": "drill_up",
                    "enabled": True,
                    "tag": "up",
                },
                {
                    "key": "Table",
                    "action": "drill_expand",
                    "enabled": True,
                    "tag": "expand",
                },
            ]

    side = SimpleNamespace(page=_Page())
    assert asyncio.run(
        vs._find_drill_control(side, "Table", "drill1", action="drill_down")
    ) is None


def test_header_initial_step_captures_default_view_without_header_dom(monkeypatch, tmp_path):
    class _Page:
        def __init__(self):
            self.shots = []

        async def evaluate(self, script, fraction):
            assert script is vs._TABLE_SCROLL_POSITION_SCRIPT
            assert fraction is None
            return {
                "scroller_count": 1,
                "controls": [{"kind": "custom", "range_ratio": 0.8}],
            }

        async def screenshot(self, path, full_page):
            self.shots.append((path, full_page))
            Path(path).write_bytes(b"png")

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vs, "_wait_for_state_render", ready)
    monkeypatch.setattr(vs, "_move_pointer_off_dashboard", ready)
    page = _Page()
    side = vs._Side("dashboard_A", "https://example", "Page", page, tmp_path)

    result = asyncio.run(vs._header_initial_step(side, tmp_path, 0))

    assert list(result["screenshots"]) == ["initial"]
    assert result["horizontal_scrollbar_present"] is True
    assert result["max_horizontal_scroll_range_ratio"] == 0.8
    assert page.shots[0][1] is False
    assert Path(page.shots[0][0]).parent.name == "Page"
    assert Path(page.shots[0][0]).parent.parent.name == "dashboard_A"
    assert Path(page.shots[0][0]).name.startswith("dashboard_A_S01_table_headers_Page01_Initial")
    assert "columnheader" not in vs._TABLE_SCROLL_POSITION_SCRIPT
    assert "innerText" not in vs._TABLE_SCROLL_POSITION_SCRIPT
    assert "node.scrollTop = scrollTop" in vs._TABLE_SCROLL_POSITION_SCRIPT


def test_header_scroll_plan_uses_incremental_overlapping_steps():
    no_scroll = {
        "screenshots": {"initial": "initial.png"},
        "horizontal_scrollbar_present": False,
        "max_horizontal_scroll_range_ratio": 0.0,
    }
    small = {
        "screenshots": {"initial": "initial.png"},
        "scroll_positions": {
            "initial": {
                "controls": [{"kind": "native", "actual_fraction": 0.0}]
            }
        },
        "horizontal_scrollbar_present": True,
        "max_horizontal_scroll_range_ratio": 0.4,
    }
    assert vs._choose_horizontal_scroll_strategy(no_scroll, no_scroll) == "NONE"
    assert vs._choose_horizontal_scroll_strategy(small, no_scroll) == "INCREMENTAL"
    assert vs._choose_horizontal_scroll_strategy({}, no_scroll) is None
    assert vs._header_scroll_fractions(small) == [1.0]
    fractions = vs._header_scroll_fractions(
        {**small, "max_horizontal_scroll_range_ratio": 1.2}
    )
    assert fractions == [1 / 3, 2 / 3, 1.0]
    assert all(
        (current - previous) * 1.2
        <= vs.HEADER_SCROLL_STEP_VIEWPORT_FRACTION
        for previous, current in zip([0.0, *fractions], fractions)
    )
    nonzero_start = {
        **small,
        "scroll_positions": {
            "initial": {
                "controls": [{"kind": "native", "actual_fraction": 0.3}]
            }
        },
        "max_horizontal_scroll_range_ratio": 1.2,
    }
    nonzero_fractions = vs._header_scroll_fractions(nonzero_start)
    assert abs(nonzero_fractions[0] - 0.65) < 1e-12
    assert nonzero_fractions[-1] == 1.0


def test_header_scroll_step_skips_unchanged_positions_and_captures_to_end(
    monkeypatch, tmp_path
):
    class _Page:
        def __init__(self):
            self.shots = []

        async def wait_for_timeout(self, _milliseconds):
            pass

        async def screenshot(self, path, full_page):
            self.shots.append((path, full_page))
            Path(path).write_bytes(b"png")

    fractions = iter([0.25, 0.25, 0.5, 1.0])

    async def set_position(_page, _fraction):
        actual = next(fractions)
        return {
            "actual_positions": [
                {"kind": "native", "actual_fraction": actual}
            ]
        }

    async def ready(*_args, **_kwargs):
        pass

    monkeypatch.setattr(vs, "_set_table_scroll_position", set_position)
    monkeypatch.setattr(vs, "_wait_for_state_render", ready)
    monkeypatch.setattr(vs, "_move_pointer_off_dashboard", ready)
    page = _Page()
    side = vs._Side("dashboard_A", "https://example", "Page", page, tmp_path)
    folder = tmp_path / "scenario"
    initial = {
        "controls": [{"kind": "native", "actual_fraction": 0.0}]
    }

    # Default: scrolling captures are taken as evidence
    result = asyncio.run(
        vs._header_scroll_step(
            side, folder, 0, [0.25, 0.5, 0.75, 1.0], initial
        )
    )

    assert result["reached_extreme_right"] is True
    assert len(page.shots) > 0
    assert all(
        Path(path).parent.name == "Page"
        and Path(path).name.startswith(
            "dashboard_A_S01_table_headers_Page01_Scroll"
        )
        for path, _ in page.shots
    )
    assert result["scroll_attempts"][1]["position_changed"] is False
    assert len(result["scroll_attempts"]) == 4

    # When disabled, scrolling still executes to extreme right without saving screenshots
    page.shots.clear()
    fractions = iter([0.25, 0.25, 0.5, 1.0])
    monkeypatch.setattr(vs, "CAPTURE_SCROLL_SCREENSHOTS", False)
    result_no_caps = asyncio.run(
        vs._header_scroll_step(
            side, folder, 0, [0.25, 0.5, 0.75, 1.0], initial
        )
    )
    assert result_no_caps["reached_extreme_right"] is True
    assert page.shots == []


def test_visual_header_comparison_accepts_conditional_screenshot_positions(monkeypatch):
    labels = [
        "Dashboard A initial",
        "Dashboard A horizontal_scroll_01",
        "Dashboard A horizontal_scroll_02",
        "Dashboard B initial",
        "Dashboard B horizontal_scroll_01",
        "Dashboard B horizontal_scroll_02",
    ]
    response = {
        "status": "FAIL",
        "tables_found_a": True,
        "tables_found_b": True,
        "headers_a": ["Facility", "Item"],
        "headers_b": ["Facility", "Product"],
        "matching_headers": ["Facility"],
        "missing_headers": [],
        "additional_headers": [],
        "renamed_headers": [
            {
                "status": "RENAMED",
                "header_a": "Item",
                "header_b": "Product",
                "observation": "The second header differs.",
            }
        ],
        "differently_ordered_headers": [],
        "notes": [],
    }

    class _Agent:
        def __init__(self, **_kwargs):
            pass

    async def fake_run(_agent, content, **_kwargs):
        assert [part.text for part in content.parts if getattr(part, "text", None)][1:] == [
            f"Screenshot: {label}" for label in labels
        ]
        return json.dumps(response)

    monkeypatch.setattr(agents, "_model_name", lambda: "test-model")
    monkeypatch.setattr(agents, "_generation_config", lambda _model: None)
    monkeypatch.setattr(agents, "LlmAgent", _Agent)
    monkeypatch.setattr(agents, "_run_agent", fake_run)

    screenshots = [(label, b"png") for label in labels]
    result = asyncio.run(agents.compare_table_headers_from_screenshots(screenshots, "All Items"))

    assert result["status"] == "FAIL"
    assert result["renamed_headers"][0]["status"] == "RENAMED"
    assert "headers_a" in result and "headers_b" in result


def test_run_page_pair_isolated_records_failure_and_continues(monkeypatch, tmp_path):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("tab not found")

    monkeypatch.setattr(vs, "_run_page_pair", boom)
    log = tmp_path / "ops.jsonl"

    result = asyncio.run(
        vs._run_page_pair_isolated(
            object(),
            page_name="Item Counts",
            destination=tmp_path,
            job_id="t",
            operation_log_path=log,
            last=vs.SCENARIO_SINGLE_FILTER,
            url_a="a",
            url_b="b",
            storage_state=None,
            settle_seconds=0,
            seed=None,
            page_semaphore=asyncio.Semaphore(1),
        )
    )

    assert result["statuses"] == {
        vs.SCENARIO_TABLE_HEADERS: vs.STATUS_UNVERIFIED,
        vs.SCENARIO_SINGLE_FILTER: vs.STATUS_UNVERIFIED,
    }
    status_path = (
        tmp_path
        / "planning"
        / "scenario_evidence"
        / "scenario_02"
        / "pages"
        / "Item_Counts"
        / "execution_status.json"
    )
    status = json.loads(status_path.read_text())
    assert status["status"] == vs.STATUS_UNVERIFIED
    assert "tab not found" in status["reason"]


def test_open_side_uses_captured_browser_mode_not_dynamic_context_list(monkeypatch, tmp_path):
    class _Page:
        async def set_viewport_size(self, _size):
            pass

    class _Context:
        async def new_page(self):
            return _Page()

    class _Browser:
        contexts = [object()]

        def __init__(self):
            self.created_contexts = 0

        async def new_context(self, **_kwargs):
            self.created_contexts += 1
            return _Context()

    async def no_reload(_self):
        pass

    monkeypatch.setattr(vs._Side, "reload", no_reload)
    browser = _Browser()
    contexts = []

    side = asyncio.run(
        vs._open_side(
            browser,
            {"viewport": vs.CAPTURE_VIEWPORT},
            "https://example",
            "Page",
            "dashboard_A",
            tmp_path,
            contexts,
            reuse_existing_context=False,
        )
    )

    assert side.label == "dashboard_A"
    assert browser.created_contexts == 1
    assert len(contexts) == 1


def test_merge_headers_ordered_handles_scrolled_overlap():
    existing = ["ID", "Name", "Category", "Status"]
    scrolled = ["Category", "Status", "Price", "Qty"]
    merged = vs._merge_headers_ordered(existing, scrolled)
    assert merged == ["ID", "Name", "Category", "Status", "Price", "Qty"]


def test_merge_headers_ordered_handles_non_overlapping():
    existing = ["Col1", "Col2"]
    scrolled = ["Col3", "Col4"]
    merged = vs._merge_headers_ordered(existing, scrolled)
    assert merged == ["Col1", "Col2", "Col3", "Col4"]


def test_table_headers_prefer_sort_by_and_deduplicate(monkeypatch, tmp_path):
    async def sort_by(*_args, **kwargs):
        kwargs["extraction_record"]["method"] = "SORT_MENU"
        kwargs["extraction_record"]["sort_by_columns"] = [
            "Item",
            "UPC",
            "Item",
        ]
        return ["Item", "UPC", "Cases", "UPC"]

    monkeypatch.setattr(vs, "_extract_sort_by_table_headers", sort_by)
    headers_record = {}

    class Page:
        async def evaluate(self, script, *_args):
            if script is vs._TABLE_SCROLL_POSITION_SCRIPT:
                return {"controls": [{"range_ratio": 1.0}]}
            raise AssertionError("DOM fallback should not run after Sort by succeeds")

    headers = asyncio.run(
        vs._extract_filtered_table_headers(
            Page(),
            tmp_path,
            headers_record,
        )
    )

    assert headers == ["Item", "UPC", "Cases"]
    assert headers_record["method"] == "SORT_MENU"
    assert headers_record["horizontal_scroll_available"] is True
    assert headers_record["sort_by_columns"] == ["Item", "UPC"]


def test_export_dialog_selects_current_layout():
    class Radio:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def wait_for(self, **_kwargs):
            return None

        async def click(self):
            self.clicked = True

    class Radios:
        def __init__(self):
            self.radio = Radio()

        async def count(self):
            return 1

        def nth(self, _index):
            return self.radio

    class Dialog:
        def __init__(self):
            self.radios = Radios()

        def get_by_role(self, role, name=None):
            assert role == "radio"
            assert name.search("Data with current layout")
            return self.radios

    dialog = Dialog()
    assert asyncio.run(browser._select_current_layout_export(dialog)) is True
    assert dialog.radios.radio.clicked is True


def test_table_headers_fall_back_to_dom_when_export_has_no_headers(
    monkeypatch, tmp_path
):
    class Page:
        async def evaluate(self, script, *_args):
            if script is vs._DOM_TABLE_HEADERS_SCRIPT:
                return ["Item", "UPC", "Item"]
            return {"controls": []}

    headers_record = {}
    headers = asyncio.run(
        vs._extract_filtered_table_headers(Page(), tmp_path, headers_record)
    )

    assert headers == ["Item", "UPC"]
    assert headers_record["method"] == "HORIZONTAL_SCROLL"
    assert headers_record["sort_by_status"] == "unavailable"
    assert headers_record["horizontal_scroll_available"] is False


def test_table_headers_fall_back_to_dom_when_scrollable_sort_by_fails(
    monkeypatch, tmp_path
):
    async def sort_by(*_args, **_kwargs):
        raise RuntimeError("Sort by menu unavailable")

    class Page:
        async def evaluate(self, script, *_args):
            if script is vs._DOM_TABLE_HEADERS_SCRIPT:
                return ["Item", "UPC"]
            return {"controls": [{"range_ratio": 1.0}]}

    monkeypatch.setattr(vs, "_extract_sort_by_table_headers", sort_by)
    headers_record = {}
    headers = asyncio.run(
        vs._extract_filtered_table_headers(Page(), tmp_path, headers_record)
    )

    assert headers == ["Item", "UPC"]
    assert headers_record["sort_by_status"] == "unavailable"
    assert headers_record["method"] == "HORIZONTAL_SCROLL"
    assert headers_record["horizontal_scroll_available"] is True


def test_sort_menu_headers_activate_table_and_never_click_a_column():
    events = []

    class Item:
        def __init__(self, text):
            self.text = text

        async def evaluate(self, _script):
            return self.text

        async def click(self):
            events.append(f"click:{self.text}")

        async def is_visible(self):
            return True

        async def wait_for(self, **_kwargs):
            return None

    class Items:
        def __init__(self, values):
            self.values = values

        async def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

        @property
        def first(self):
            return self.values[0]

        async def evaluate_all(self, _script):
            return ["Product", "Quantity"]

    class Menu:
        @property
        def last(self):
            return self

        async def is_visible(self):
            return True

        async def wait_for(self, **_kwargs):
            return None

        def locator(self, selector):
            if selector == "button, [role=\"menuitem\"]":
                return Items([Item("Sort by")])
            if selector == 'button[data-automation-type="dropdown-item"]':
                return Items([Item("Product"), Item("Quantity")])
            raise AssertionError(selector)

        async def evaluate(self, _script):
            return False

    class Cell:
        async def is_visible(self):
            return True

        async def click(self, **_kwargs):
            events.append("cell")

    class Container:
        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            return {"id": "table-1", "title": "Inventory"}

        def locator(self, _selector):
            return Items([Cell()])

        def get_by_role(self, role, name=None):
            assert role == "button"
            assert name.search("more options")
            return Items([MoreButton()])

    class MoreButton:
        async def is_visible(self):
            events.append("more-options-checked")
            return True

        async def is_enabled(self):
            return True

        async def click(self):
            events.append("more-options-clicked")

    class Page:
        def locator(self, selector):
            if selector == vs.VISUAL_SELECTOR:
                return Items([Container()])
            return Menu()

    record = {}
    headers = asyncio.run(
        vs._extract_sort_by_table_headers(Page(), extraction_record=record)
    )

    assert headers == ["Product", "Quantity"]
    assert record["visuals"] == [
        {
            "title": "Inventory",
            "visual_id": "table-1",
            "headers": ["Product", "Quantity"],
            "method": "SORT_MENU",
        }
    ]
    assert events.index("cell") < events.index("more-options-checked")
    assert events == [
        "cell",
        "more-options-checked",
        "more-options-clicked",
        "click:Sort by",
    ]


def test_header_fallback_scrolls_incrementally_and_records_each_table(
    monkeypatch,
):
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("Sort menu unavailable")

    monkeypatch.setattr(vs, "_extract_sort_by_table_headers", unavailable)

    class Container:
        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            visible_headers = (
                ["SKU"]
                if page.fraction == 0
                else ["SKU", "Description"]
                if page.fraction < 0.75
                else ["Description", "On Hand"]
            )
            return {
                "visual_id": "table-1",
                "title": "Inventory",
                "headers": visible_headers,
            }

    class Containers:
        async def count(self):
            return 1

        def nth(self, _index):
            return Container()

    class Page:
        fraction = 0

        def __init__(self):
            self.scroll_steps = []

        def locator(self, _selector):
            return Containers()

        async def evaluate(self, _script, fraction=None):
            if fraction is not None:
                self.fraction = fraction
                self.scroll_steps.append(fraction)
            return {
                "controls": [
                    {
                        "kind": "native",
                        "range_ratio": 1.0,
                        "actual_fraction": self.fraction,
                    }
                ]
            }

    page = Page()
    record = {}
    headers = asyncio.run(
        vs._extract_filtered_table_headers(page, extraction_record=record)
    )

    assert headers == ["SKU", "Description", "On Hand"]
    assert page.scroll_steps == [0.25, 0.5, 0.75, 1.0, 0.0]
    assert record["visuals"] == [
        {
            "title": "Inventory",
            "visual_id": "table-1",
            "headers": ["SKU", "Description", "On Hand"],
            "method": "HORIZONTAL_SCROLL",
        }
    ]


def test_check_and_clear_filters_preserves_when_button_absent(monkeypatch, tmp_path):
    async def mock_clear_button(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vs, "_clear_selection_button", mock_clear_button)
    side = vs._Side("dashboard_A", "https://example", "Page", object(), tmp_path)
    side.filters = [{"name": "Department", "selected": ["All"]}]
    actions = asyncio.run(vs._check_and_clear_filters(side))
    assert len(actions) == 1
    assert actions[0]["filter"] == "Department"
    assert actions[0]["clear_control_found"] is False
    assert actions[0]["action"] == "preserved"


def test_check_and_clear_filters_updates_baseline_after_success(
    monkeypatch, tmp_path
):
    async def mock_clear_button(*_args, **_kwargs):
        return object()

    async def mock_click(*_args, **_kwargs):
        return True

    async def mock_current_value(*_args, **_kwargs):
        return []

    monkeypatch.setattr(vs, "_clear_selection_button", mock_clear_button)
    monkeypatch.setattr(vs, "_click_clear_selection_button", mock_click)
    monkeypatch.setattr(vs, "_current_filter_value", mock_current_value)
    class FakePage:
        async def wait_for_timeout(self, _milliseconds):
            return None

    side = vs._Side("dashboard_A", "https://example", "Page", FakePage(), tmp_path)
    side.filters = [{"name": "Department", "selected": ["North"]}]

    actions = asyncio.run(vs._check_and_clear_filters(side))

    assert actions[0]["action"] == "cleared"
    assert side.filters[0]["selected"] == []
    assert side.filters[0]["reset_strategy"] == "clear_selection"
    assert side.filters[0]["clear_selection_checked"] is True

