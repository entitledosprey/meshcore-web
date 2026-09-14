"""InfluxDB line protocol encoding.

Hand-formatting line protocol is a trap here. Tag values on these measurements
come from node names, which are set by whoever owns the node -- spaces and
commas are common, equals signs and quotes happen. An unescaped one does not
raise; it silently shifts a field into a tag or truncates the point, and the
damage only shows up as a confusing dashboard weeks later.
"""
from typing import Any, Mapping

# Line protocol terminates a point at a newline, so control characters in a
# string field would split one point into two malformed ones.
_CONTROL = {c: " " for c in range(0x20)}
_CONTROL[0x7F] = " "


def _clean(s: str) -> str:
    return str(s).translate(_CONTROL)


def esc_measurement(s: str) -> str:
    return _clean(s).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def esc_key(s: str) -> str:
    """Tag keys, tag values and field keys share one escaping rule."""
    return (_clean(s).replace("\\", "\\\\").replace(",", "\\,")
            .replace("=", "\\=").replace(" ", "\\ "))


def esc_str_field(s: str) -> str:
    return '"' + _clean(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def fmt_field(v: Any) -> str | None:
    """Render one field value, or None if it cannot be represented.

    bool is checked before int deliberately -- bool is a subclass of int, and
    writing True as 1i changes the column type Grafana sees.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        # NaN and infinities are not representable; dropping the field is
        # better than poisoning the write with a rejected point.
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return repr(v)
    return esc_str_field(str(v))


def point(measurement: str, tags: Mapping[str, Any], fields: Mapping[str, Any],
          ts_ns: int) -> str | None:
    """Build one line, or None when there is nothing worth writing.

    Empty tag values are dropped rather than written: InfluxDB treats an empty
    tag value as the tag being absent, so emitting one creates a second series
    that looks identical in a dashboard legend but never joins up.
    """
    parts = [esc_measurement(measurement)]
    for k, v in tags.items():
        if v is None:
            continue
        sv = _clean(v).strip()
        if not sv:
            continue
        parts.append(f"{esc_key(k)}={esc_key(sv)}")

    rendered = []
    for k, v in fields.items():
        fv = fmt_field(v)
        if fv is not None:
            rendered.append(f"{esc_key(k)}={fv}")
    if not rendered:
        return None       # a point with no fields is rejected by InfluxDB

    return f"{','.join(parts)} {','.join(rendered)} {int(ts_ns)}"
