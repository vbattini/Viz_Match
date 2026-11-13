from __future__ import annotations

import re
from statistics import median
from typing import Any


STANDARD_ELEMENT_NAMES = (
    "dashboard_title",
    "report_title",
    "brand_logo",
    "page_title",
    "page_navigation",
    "visual_title",
    "chart_title",
    "table_title",
    "matrix_title",
    "kpi_title",
    "section_title",
    "slicer_title",
    "slicer_value",
    "filter_title",
    "x_axis_title",
    "y_axis_title",
    "secondary_axis_title",
    "legend_title",
    "legend_item",
    "column_header",
    "row_header",
    "visual_subtitle",
    "tooltip_title",
    "button_label",
    "icon",
    "navigation_label",
    "annotation_title",
    "metadata_label",
    "metadata_value",
    "export_status",
    "export_message",
    "application_label",
    "instruction_text",
)

_ALIASES = {
    "dashboardtitle": "dashboard_title",
    "reporttitle": "report_title",
    "brandlogo": "brand_logo",
    "pagetitle": "page_title",
    "pagenavigation": "page_navigation",
    "pagenavigationlabel": "page_navigation",
    "visualtitle": "visual_title",
    "charttitle": "chart_title",
    "tabletitle": "table_title",
    "matrixtitle": "matrix_title",
    "kpititle": "kpi_title",
    "sectiontitle": "section_title",
    "slicertitle": "slicer_title",
    "slicervalue": "slicer_value",
    "filtertitle": "filter_title",
    "filtervalue": "slicer_value",
    "xaxistitle": "x_axis_title",
    "yaxistitle": "y_axis_title",
    "secondaryaxistitle": "secondary_axis_title",
    "legendtitle": "legend_title",
    "legenditem": "legend_item",
    "columnheader": "column_header",
    "rowheader": "row_header",
    "visualsubtitle": "visual_subtitle",
    "subtitle": "visual_subtitle",
    "tooltiptitle": "tooltip_title",
    "buttonlabel": "button_label",
    "icon": "icon",
    "navigationlabel": "navigation_label",
    "annotationtitle": "annotation_title",
    "metadata": "metadata_label",
    "metadatalabel": "metadata_label",
    "metadatavalue": "metadata_value",
    "exportstatus": "export_status",
    "exportmessage": "export_message",
    "applicationlabel": "application_label",
    "instruction": "instruction_text",
    "instructiontext": "instruction_text",
}


def _signature(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def standard_element_name(
    category: Any = None,
    element_type: Any = None,
    text: Any = None,
) -> str:
    signature = _signature(category)
    if signature in _ALIASES:
        return _ALIASES[signature]
    if signature in {_signature(name) for name in STANDARD_ELEMENT_NAMES}:
        return str(category).strip().casefold().replace("-", "_").replace(" ", "_")

    type_signature = _signature(element_type)
    type_names = {
        "chart": "chart_title",
        "barchart": "chart_title",
        "linechart": "chart_title",
        "columnchart": "chart_title",
        "table": "table_title",
        "matrix": "matrix_title",
        "kpi": "kpi_title",
        "card": "kpi_title",
        "slicer": "slicer_title",
        "slicervalue": "slicer_value",
        "filter": "filter_title",
        "navigation": "page_navigation",
        "button": "button_label",
        "metadata": "metadata_label",
        "application": "application_label",
    }
    return type_names.get(type_signature, "visual_title")


def reading_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) < 2:
        return list(items)
    boxes = []
    for item in items:
        box = item.get("bounding_box")
        if not (
            isinstance(box, dict)
            and isinstance(box.get("x"), (int, float))
            and isinstance(box.get("y"), (int, float))
        ):
            box = item.get("normalized_bounding_box")
        boxes.append(box)
    if not all(
        isinstance(box, dict)
        and isinstance(box.get("x"), (int, float))
        and isinstance(box.get("y"), (int, float))
        for box in boxes
    ):
        orders = [item.get("reading_order") for item in items]
        if all(
            isinstance(order, int) and not isinstance(order, bool) and order >= 1
            for order in orders
        ):
            return [
                item
                for _, item in sorted(
                    enumerate(items),
                    key=lambda indexed: (indexed[1]["reading_order"], indexed[0]),
                )
            ]
        return list(items)

    heights = [
        float(box["height"])
        for box in boxes
        if isinstance(box.get("height"), (int, float)) and box["height"] > 0
    ]
    normalized_coordinates = all(
        0 <= float(box["x"]) <= 1
        and 0 <= float(box["y"]) <= 1
        and (
            not isinstance(box.get("width"), (int, float))
            or 0 <= float(box["width"]) <= 1
        )
        and (
            not isinstance(box.get("height"), (int, float))
            or 0 <= float(box["height"]) <= 1
        )
        for box in boxes
    )
    minimum_tolerance = 0.01 if normalized_coordinates else 1.0
    row_tolerance = (
        max(minimum_tolerance, median(heights) * 0.6)
        if heights
        else minimum_tolerance
    )
    positioned = []
    for index, (item, box) in enumerate(zip(items, boxes)):
        height = (
            float(box["height"])
            if isinstance(box.get("height"), (int, float)) and box["height"] > 0
            else row_tolerance
        )
        positioned.append(
            {
                "index": index,
                "item": item,
                "x": float(box["x"]),
                "y": float(box["y"]),
                "center_y": float(box["y"]) + height / 2,
                "height": height,
            }
        )
    positioned.sort(key=lambda item: (item["center_y"], item["x"], item["index"]))

    rows: list[dict[str, Any]] = []
    for current in positioned:
        matching_row = next(
            (
                row
                for row in rows
                if abs(current["center_y"] - row["center_y"])
                <= max(row_tolerance, current["height"] * 0.5, row["height"] * 0.5)
            ),
            None,
        )
        if matching_row is None:
            rows.append(
                {
                    "center_y": current["center_y"],
                    "height": current["height"],
                    "items": [current],
                }
            )
        else:
            matching_row["items"].append(current)
            matching_row["center_y"] = sum(
                item["center_y"] for item in matching_row["items"]
            ) / len(matching_row["items"])
            matching_row["height"] = max(
                item["height"] for item in matching_row["items"]
            )

    rows.sort(key=lambda row: min(item["y"] for item in row["items"]))
    return [
        item["item"]
        for row in rows
        for item in sorted(row["items"], key=lambda value: (value["x"], value["index"]))
    ]
