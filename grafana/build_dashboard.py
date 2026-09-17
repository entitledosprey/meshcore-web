#!/usr/bin/env python3
"""Generate the MeshCore platform Grafana dashboard.

Written as a generator rather than hand-edited JSON so the Flux queries stay
readable and can be extracted and run against InfluxDB before import -- a
mistyped query in a 20-panel dashboard is otherwise only found by eye, one
panel at a time.

    python3 grafana/build_dashboard.py > grafana/meshcore-platform.json
"""
import json

DS = {"type": "influxdb", "uid": "cf9lcoakbb9j4a"}

# Categorical slots 1-7 of the validated palette, dark steps. Colours are bound
# to payload types in ALPHABETICAL order on purpose: Flux emits groups sorted by
# key, so alphabetical order is the order the stack renders in, and this mapping
# puts the palette's validated adjacent-pair chain on screen in that same order.
# Re-ordering these without re-validating puts yellow next to orange, the one
# pair in this palette that fails on its own.
SLOT = ["#3987e5", "#d95926", "#199e70", "#c98500",
        "#d55181", "#008300", "#9085e9"]
PTYPES = ["ADVERT", "ANON_REQ", "GRP_TXT", "Other", "PATH", "REQ", "RESPONSE"]
PTYPE_COLOR = dict(zip(PTYPES, SLOT))

# Sequential blue, ordinal range (no lighter than step 250 so every bar clears
# 2:1 against the panel surface).
BLUE_ORDINAL = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
                "#2a78d6", "#256abf", "#1c5cab", "#184f95"]

# CARTO's basemaps (Grafana's "default" basemap) now bake an "API KEY REQUIRED"
# watermark into every tile and still return HTTP 200, so a status-code check
# does not catch it -- the map just quietly renders branded. OpenStreetMap's
# public tiles refuse unidentified clients with an "Access blocked" image, also
# at HTTP 200. Esri's dark gray canvas serves clean tiles with no key and suits
# a dark dashboard, so it is pinned explicitly rather than left to whatever
# Grafana's default happens to be.
BASEMAP = {
    "type": "xyz",
    "name": "Basemap",
    "config": {
        "url": "https://services.arcgisonline.com/ArcGIS/rest/services/"
               "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
    },
}

# Pinned rather than fit-to-data: a fit view is only ever as good as the worst
# coordinate in the set, so one bad advert reframes the whole map. Centred on
# the Hill Country / Austin corridor where the mesh actually lives.
VIEW = {"id": "coords", "lat": 30.3, "lon": -97.9, "zoom": 8}

# Series naming: after CLEAN the only label left is the identity tag.
REPEATER_NAME = "${__field.labels.repeater}"
NODE_NAME = "${__field.labels.node}"

GOOD, CRITICAL, WARNING = "#0ca30c", "#d03b3b", "#fab219"

# Payload types charted individually. Fixed, not top-N: a top-N list repaints
# every series whenever the ranking shifts, so a colour would stop meaning a
# particular kind of traffic.
NAMED_PTYPES = '["REQ", "PATH", "ANON_REQ", "GRP_TXT", "ADVERT", "RESPONSE"]'

# Flux keeps _start/_stop/_measurement/_field in the group key, and Grafana
# builds a series name out of every group-key column -- which is where legends
# like `_value {_start="2026-09-14T..."}` come from. Dropping them leaves only
# the identity tag, so displayName can render a bare, stable name. This also
# makes byName colour overrides match: against the long generated name they
# never did, so the palette silently fell back to Grafana's own colours.
CLEAN = ('\n  |> drop(columns: ["_start", "_stop", "_measurement", "_field"])')

RX = '''  |> filter(fn: (r) => r._measurement == "mc_rx")
  |> filter(fn: (r) => r.node =~ /^${node:regex}$/)'''

HEAD = 'from(bucket: "${bucket}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'


def q_frames_total():
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "len")
  |> group()
  |> count()''' + CLEAN


def q_unique_packets():
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "len" and r.dup == "0")
  |> group()
  |> count()''' + CLEAN


def q_repeat_rate():
    # One pass rather than a join: reduce carries both counters, so the ratio
    # cannot disagree with itself across two separately-evaluated queries.
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "len")
  |> group()
  |> reduce(
      identity: {total: 0.0, dups: 0.0},
      fn: (r, accumulator) => ({
        total: accumulator.total + 1.0,
        dups: accumulator.dups + (if r.dup == "1" then 1.0 else 0.0),
      }))
  |> map(fn: (r) => ({_value: if r.total > 0.0 then 100.0 * r.dups / r.total else 0.0}))'''


def q_adverts():
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "len" and r.ptype == "ADVERT")
  |> group()
  |> count()''' + CLEAN


def q_nodes_heard():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_node" and r._field == "heard")
  |> group(columns: ["pubkey"])
  |> last()
  |> group()
  |> count()'''


def q_frames_by_type():
    return HEAD + RX + f'''
  |> filter(fn: (r) => r._field == "len")
  |> map(fn: (r) => ({{r with ptype:
      if contains(value: r.ptype, set: {NAMED_PTYPES}) then r.ptype else "Other"}}))
  |> group(columns: ["ptype"])
  |> aggregateWindow(every: v.windowPeriod, fn: count, createEmpty: true)
  |> map(fn: (r) => ({{r with _value: if exists r._value then r._value else 0}}))''' + CLEAN


def q_frames_by_hops():
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "len")
  |> group(columns: ["hops"])
  |> count()
  |> group()
  |> map(fn: (r) => ({r with order: if r.hops =~ /^[0-9]+$/ then int(v: r.hops) else 99}))
  |> sort(columns: ["order"])
  |> keep(columns: ["hops", "_value"])
  |> rename(columns: {_value: "frames"})'''


SNR_BUCKET_DB = 2.0


def q_snr_heatmap():
    """SNR distribution, bucketed in InfluxDB rather than in the browser.

    Sending one point per heard frame does not scale: Grafana truncated this
    panel at 6961 points (it can draw ~696 at this width), so the picture was
    silently incomplete. Counting into fixed dB buckets server-side keeps each
    series far under that and, unlike averaging, preserves the shape -- the
    distribution here is bimodal, which is the whole reason to draw it.

    Emitted as pre-bucketed "time series buckets": one series per dB bucket,
    named by its lower bound, which is the format the heatmap panel reads when
    `calculate` is off.
    """
    return ('import "math"\n\n' + HEAD + RX + f'''
  |> filter(fn: (r) => r._field == "snr")
  |> map(fn: (r) => ({{r with snr_bucket:
      string(v: int(v: math.floor(x: r._value / {SNR_BUCKET_DB}) * {SNR_BUCKET_DB}))}}))
  |> group(columns: ["snr_bucket"])
  |> aggregateWindow(every: v.windowPeriod, fn: count, createEmpty: false)''') + CLEAN


def q_snr_by_hops():
    return HEAD + RX + '''
  |> filter(fn: (r) => r._field == "snr")
  |> group(columns: ["hops"])
  |> mean()
  |> group()
  |> map(fn: (r) => ({r with order: if r.hops =~ /^[0-9]+$/ then int(v: r.hops) else 99}))
  |> sort(columns: ["order"])
  |> keep(columns: ["hops", "_value"])
  |> rename(columns: {_value: "mean SNR"})'''


def q_local(field: str):
    return HEAD + f'''  |> filter(fn: (r) => r._measurement == "mc_local" and r._field == "{field}")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)''' + CLEAN


def q_node_table():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_node" and r._field == "snr")
  |> group(columns: ["pubkey", "name", "type"])
  |> last()
  |> group()
  |> sort(columns: ["_time"], desc: true)
  |> keep(columns: ["_time", "pubkey", "name", "type", "_value"])
  |> rename(columns: {_time: "last heard", _value: "SNR"})'''


def q_node_map():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_node")
  |> filter(fn: (r) => r._field == "lat" or r._field == "lon")
  |> group(columns: ["pubkey", "name", "type", "_field"])
  |> last()
  |> group(columns: ["pubkey", "name", "type"])
  |> pivot(rowKey: ["pubkey"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> keep(columns: ["pubkey", "name", "type", "lat", "lon"])
  // Nodes that set the "has location" advert flag but never got a fix report
  // 0,0. Left in, a single one of them drags a fit-to-data view out to the
  // Gulf of Guinea and the mesh becomes an unreadable dot.
  |> filter(fn: (r) => not (r.lat > -0.01 and r.lat < 0.01
                            and r.lon > -0.01 and r.lon < 0.01))'''


def q_repeater(field: str, agg: str = "last"):
    return HEAD + f'''  |> filter(fn: (r) => r._measurement == "mc_repeater" and r._field == "{field}")
  |> group(columns: ["repeater"])
  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)''' + CLEAN


def q_repeater_table():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_repeater")
  |> filter(fn: (r) => r._field == "uptime" or r._field == "bat"
                    or r._field == "tx_queue_len" or r._field == "nb_recv"
                    or r._field == "flood_dups" or r._field == "rtt_ms")
  |> group(columns: ["repeater", "_field"])
  |> last()
  |> pivot(rowKey: ["repeater"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> keep(columns: ["repeater", "uptime", "bat", "tx_queue_len", "nb_recv",
                    "flood_dups", "rtt_ms"])'''


def q_repeater_telem(type_name: str):
    """One sensor type over time.

    Split by type rather than charted together because LPP telemetry mixes
    units freely -- degrees, volts, percent -- and putting them on one axis
    would need a second y-scale, which is never the right answer.
    """
    return HEAD + (
        '  |> filter(fn: (r) => r._measurement == "mc_repeater_telem")\n'
        '  |> filter(fn: (r) => r.type == "%s")\n'
        '  |> group(columns: ["repeater", "channel"])\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: last, '
        'createEmpty: false)' % type_name) + CLEAN


def q_repeater_telem_table():
    """Every sensor reading, whatever its type.

    Temperature and battery voltage are what a bare repeater reports; anything
    with external sensors attached shows up here too, without needing a new
    panel per sensor type.
    """
    return HEAD + (
        '  |> filter(fn: (r) => r._measurement == "mc_repeater_telem")\n'
        '  |> group(columns: ["repeater", "type", "channel"])\n'
        '  |> last()\n'
        '  |> group()\n'
        '  |> keep(columns: ["repeater", "type", "channel", "_value", "_time"])\n'
        '  |> rename(columns: {_value: "value", _time: "reading"})\n'
        '  |> sort(columns: ["repeater", "type"])')


def q_neighbours():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_neighbour" and r._field == "snr")
  |> group(columns: ["repeater", "neighbour"])
  |> last()
  |> group()
  |> sort(columns: ["_value"], desc: true)
  |> keep(columns: ["repeater", "neighbour_name", "neighbour", "_value", "_time"])
  |> rename(columns: {_value: "SNR", _time: "seen", neighbour_name: "heard",
                      neighbour: "key"})'''


def q_collector(field: str, agg: str = "last"):
    return HEAD + f'''  |> filter(fn: (r) => r._measurement == "mc_collector" and r._field == "{field}")
  |> group(columns: ["node", "_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)''' + CLEAN


def q_serial_up():
    return HEAD + '''  |> filter(fn: (r) => r._measurement == "mc_collector" and r._field == "serial_up")
  |> last()
  |> map(fn: (r) => ({r with _value: if r._value then 1 else 0}))
  |> group()'''


# --------------------------------------------------------------------------
# panel helpers
# --------------------------------------------------------------------------

_id = [0]


def nid() -> int:
    _id[0] += 1
    return _id[0]


def targets(*queries):
    return [{"datasource": DS, "query": q, "refId": chr(65 + i)}
            for i, q in enumerate(queries)]


def row(title, y):
    return {"type": "row", "title": title, "id": nid(), "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def stat(title, query, x, y, w=4, h=4, unit="short", decimals=None,
         desc="", thresholds=None, mappings=None):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "type": "stat", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "mappings": mappings or [],
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": steps},
        }, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto", "colorMode": "value", "graphMode": "none",
            "justifyMode": "auto", "orientation": "auto",
        },
    }


def timeseries(title, query, x, y, w, h, *, unit="short", stack=False,
               fill=0, desc="", overrides=None, legend_table=False,
               draw="line", width=2, points=False, minval=None,
               display_name=None, fixed_color=None):
    custom = {
        "drawStyle": draw,
        "lineWidth": width,
        "fillOpacity": fill,
        "showPoints": "auto" if points else "never",
        "pointSize": 8,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "axisBorderShow": False,
        "gradientMode": "none",
        "barAlignment": 0,
        "lineInterpolation": "linear",
        "spanNulls": False,
        # A 2px gap between stacked fills keeps adjacent segments from reading
        # as one shape.
        "barWidthFactor": 0.9,
    }
    color = ({"mode": "fixed", "fixedColor": fixed_color} if fixed_color
             else {"mode": "palette-classic"})
    defaults = {"unit": unit, "custom": custom, "color": color,
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "text", "value": None}]}}
    if display_name is not None:
        # Renders the series as just its identity -- "ADVERT", "Osprey-HF-Kyle"
        # -- instead of _value plus the whole label set.
        defaults["displayName"] = display_name
    if minval is not None:
        defaults["min"] = minval
    return {
        "type": "timeseries", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "table" if legend_table else "list",
                       "placement": "bottom", "showLegend": True,
                       "calcs": ["mean", "max"] if legend_table else []},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def color_override(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color",
                            "value": {"mode": "fixed", "fixedColor": color}}]}


def barchart(title, query, x, y, w, h, *, xfield, unit="short", desc="",
             overrides=None, color=None):
    return {
        "type": "barchart", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        "fieldConfig": {"defaults": {
            "unit": unit,
            "color": {"mode": "fixed", "fixedColor": color or SLOT[0]},
            "custom": {"lineWidth": 0, "fillOpacity": 90, "radius": 0,
                       "barRadius": 0.15, "axisBorderShow": False,
                       "gradientMode": "none"},
            "thresholds": {"mode": "absolute",
                           "steps": [{"color": "text", "value": None}]},
        }, "overrides": overrides or []},
        "options": {
            "xField": xfield,
            "orientation": "auto",
            "showValue": "never",
            "stacking": "none",
            "legend": {"showLegend": False, "displayMode": "list",
                       "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def table(title, query, x, y, w, h, *, desc="", overrides=None):
    return {
        "type": "table", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        "transformations": [{"id": "merge", "options": {}}],
        "fieldConfig": {"defaults": {
            "custom": {"align": "auto", "filterable": True},
            "thresholds": {"mode": "absolute",
                           "steps": [{"color": "text", "value": None}]},
        }, "overrides": overrides or []},
        "options": {"showHeader": True, "footer": {"show": False}},
    }


def heatmap(title, query, x, y, w, h, *, desc="", min_interval=None,
            bucket_label=None):
    panel = {
        "type": "heatmap", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        # Floors v.windowPeriod so the query cannot return one column per pixel
        # when the dashboard is on a short time range.
        "interval": min_interval,
        "options": {
            # Off: the buckets are computed in Flux, so the panel must read them
            # rather than re-bucket raw points it was never sent.
            "calculate": False,
            # One hue, light to dark. A rainbow scheme here would imply
            # categories where there is only magnitude.
            "color": {"mode": "scheme", "scheme": "Blues", "steps": ytosteps(),
                      "reverse": False, "exponent": 0.5, "fill": SLOT[0]},
            "cellGap": 2,
            "yAxis": {"unit": "dB", "axisPlacement": "left"},
            "legend": {"show": True},
            "tooltip": {"mode": "single", "yHistogram": True, "showColorScale": True},
            "exemplars": {"color": "rgba(255,0,255,0.7)"},
            "filterValues": {"le": 1e-9},
            "rowsFrame": {"layout": "auto"},
            "showValue": "never",
        },
        "fieldConfig": {"defaults": {
            "custom": {"hideFrom": {"legend": False, "tooltip": False,
                                    "viz": False}},
            # The series name IS the bucket's lower bound in this format.
            "displayName": bucket_label,
        }, "overrides": []},
    }
    if min_interval is None:
        del panel["interval"]
    return panel


def ytosteps():
    return 64


def geomap(title, query, x, y, w, h, *, desc=""):
    return {
        "type": "geomap", "title": title, "id": nid(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets(query),
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": SLOT[0]},
            "thresholds": {"mode": "absolute",
                           "steps": [{"color": "text", "value": None}]},
        }, "overrides": []},
        "options": {
            "view": VIEW,
            "basemap": BASEMAP,
            "layers": [{
                "type": "markers", "name": "Nodes",
                "location": {"mode": "coords", "latitude": "lat",
                             "longitude": "lon"},
                "config": {
                    "style": {
                        "size": {"fixed": 9, "min": 6, "max": 14},
                        "color": {"fixed": SLOT[0]},
                        "opacity": 0.9,
                        "symbol": {"mode": "fixed",
                                   "fixed": "img/icons/marker/circle.svg"},
                        "textConfig": {"fontSize": 12, "offsetX": 0,
                                       "offsetY": -14, "textAlign": "center",
                                       "textBaseline": "middle"},
                    },
                    "showLegend": False,
                },
                "tooltip": True,
            }],
            "controls": {"showZoom": True, "showAttribution": True,
                         "mouseWheelZoom": False},
            "tooltip": {"mode": "details"},
        },
    }


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

def build() -> dict:
    p = []
    ptype_overrides = [color_override(t, PTYPE_COLOR[t]) for t in PTYPES]

    p.append(row("Air", 0))
    y = 1
    p.append(stat("Frames heard", q_frames_total(), 0, y,
                  desc="Every frame the radio demodulated, repeats included -- "
                       "this is what the channel actually cost in airtime."))
    p.append(stat("Unique packets", q_unique_packets(), 4, y,
                  desc="Frames whose packet hash was not already seen inside "
                       "the dedupe window: the real traffic behind the airtime."))
    p.append(stat("Repeated", q_repeat_rate(), 8, y, unit="percent", decimals=1,
                  desc="Share of frames that were rebroadcasts of a packet "
                       "already heard. High is normal in a dense flood mesh."))
    p.append(stat("Adverts", q_adverts(), 12, y,
                  desc="Advert frames -- the ones carrying node identity."))
    p.append(stat("Nodes heard", q_nodes_heard(), 16, y,
                  desc="Distinct public keys seen advertising."))
    p.append(stat("Radio link", q_serial_up(), 20, y,
                  desc="Whether the collector currently holds the serial link.",
                  mappings=[{"type": "value", "options": {
                      "0": {"text": "DOWN", "color": CRITICAL, "index": 0},
                      "1": {"text": "UP", "color": GOOD, "index": 1}}}],
                  thresholds=[{"color": "text", "value": None}]))

    y += 4
    p.append(timeseries(
        "Frames by payload type", q_frames_by_type(), 0, y, 16, 9,
        stack=True, fill=85, draw="bars", width=1, legend_table=True,
        overrides=ptype_overrides, minval=0,
        display_name="${__field.labels.ptype}",
        desc="Stacked count per interval. Types beyond the six charted "
             "individually are folded into Other so a colour always means the "
             "same kind of traffic."))
    p.append(barchart(
        "Frames by hop count", q_frames_by_hops(), 16, y, 8, 9,
        xfield="hops", color=SLOT[0],
        desc="How far traffic has travelled before reaching this receiver. "
             "0 means heard directly from the sender."))

    y += 9
    p.append(row("RF quality", y))
    y += 1
    p.append(heatmap("SNR distribution", q_snr_heatmap(), 0, y, 12, 9,
                     min_interval="10m",
                     bucket_label="${__field.labels.snr_bucket}",
                     desc=f"Frames counted into {SNR_BUCKET_DB:g} dB bands. "
                          "Distinct horizontal bands are distinct sets of "
                          "neighbours, which is why this is a distribution and "
                          "not an average."))
    p.append(barchart("Mean SNR by hop count", q_snr_by_hops(), 12, y, 6, 9,
                      xfield="hops", unit="dB", color=SLOT[0],
                      desc="Signal quality does not decay with hop count -- "
                           "each hop is its own local link -- so a slope here "
                           "says something about which repeaters are reaching us."))
    p.append(timeseries("Local radio noise floor", q_local("noise_floor"),
                        18, y, 6, 9, unit="dBm",
                        display_name="Noise floor", fixed_color=SLOT[0],
                        desc="From the companion radio's own stats. Empty "
                             "until the collector runs against real hardware."))

    y += 9
    p.append(row("Nodes", y))
    y += 1
    p.append(table("Node registry", q_node_table(), 0, y, 12, 10,
                   desc="Built from overheard adverts. Keyed on public key, so "
                        "a node that renames itself stays one row."))
    p.append(geomap("Node locations", q_node_map(), 12, y, 12, 10,
                    desc="Only nodes that advertise coordinates appear."))

    y += 10
    p.append(row("Repeaters", y))
    y += 1
    p.append(timeseries("Battery", q_repeater("bat"), 0, y, 6, 8, unit="mvolt",
                        legend_table=True, display_name=REPEATER_NAME,
                        desc="From the binary status request. The telemetry "
                             "path reports the same cell in volts, which is a "
                             "free cross-check that both decoders agree."))
    p.append(timeseries("Temperature", q_repeater_telem("temperature"),
                        6, y, 6, 8, unit="celsius", legend_table=True,
                        display_name=REPEATER_NAME,
                        desc="MCU temperature, from the LPP telemetry request. "
                             "The binary status struct has no temperature "
                             "field, so this is the only path that carries it."))
    p.append(timeseries("TX queue depth", q_repeater("tx_queue_len"),
                        12, y, 6, 8, legend_table=True,
                        display_name=REPEATER_NAME,
                        desc="A queue that does not drain means the repeater is "
                             "transmitting slower than it is being asked to."))
    p.append(timeseries("Round-trip time", q_repeater("rtt_ms"), 18, y, 6, 8,
                        unit="ms", legend_table=True, display_name=REPEATER_NAME,
                        desc="How long a status request took over the mesh."))
    y += 8
    p.append(table("Repeater status", q_repeater_table(), 0, y, 8, 8,
                   desc="Latest reading per repeater you own and have "
                        "credentials for."))
    p.append(table("Repeater sensors", q_repeater_telem_table(), 8, y, 8, 8,
                   desc="Latest value for every LPP telemetry channel, "
                        "including any external sensors."))
    p.append(table("Repeater neighbours", q_neighbours(), 16, y, 8, 8,
                   desc="Zero-hop neighbours each repeater can hear, named "
                        "from overheard adverts and the radio's contact list. "
                        "The key column falls back to the raw prefix when the "
                        "node has not been heard advertising yet."))
    y += 8
    p.append(row("Collector health", y))
    y += 1
    p.append(timeseries("Points written", q_collector("points_written"),
                        0, y, 8, 7, legend_table=True,
                        display_name=NODE_NAME, fixed_color=SLOT[0]))
    p.append(timeseries("Points dropped", q_collector("points_dropped"),
                        8, y, 8, 7, legend_table=True,
                        desc="Non-zero means writes are being rejected or the "
                             "buffer overflowed. Should stay flat.",
                        display_name=NODE_NAME, fixed_color=CRITICAL))
    p.append(timeseries("Spool on disk", q_collector("spool_bytes"),
                        16, y, 8, 7, unit="bytes", legend_table=True,
                        desc="Growing means InfluxDB is unreachable but nothing "
                             "is being lost yet.",
                        display_name=NODE_NAME, fixed_color=WARNING))

    return {
        "uid": "meshcore-platform",
        "title": "MeshCore Platform",
        "description": "Heard traffic, node registry and repeater telemetry "
                       "from a MeshCore companion radio on USB serial.",
        "tags": ["meshcore", "lora"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "editable": True,
        "graphTooltip": 1,
        "templating": {"list": [
            {"name": "bucket", "type": "textbox", "label": "Bucket",
             "query": "meshcore", "current": {"text": "meshcore",
                                              "value": "meshcore"},
             "options": [], "hide": 0},
            {"name": "node", "type": "query", "label": "Receiver",
             "datasource": DS, "refresh": 1, "multi": True, "includeAll": True,
             "allValue": ".*", "current": {"text": "All", "value": "$__all"},
             "options": [], "hide": 0,
             "query": 'import "influxdata/influxdb/schema"\n'
                      'schema.tagValues(bucket: "${bucket}", tag: "node", '
                      'predicate: (r) => r._measurement == "mc_rx")'},
        ]},
        "panels": p,
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
