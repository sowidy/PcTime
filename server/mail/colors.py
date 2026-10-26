"""Shared soft application/site palette and timeline intensity."""
import colorsys

PALETTE = ("#a7c4e5", "#accfbd", "#c5b6dd", "#edc2a8", "#deb5c5", "#c9d5a8",
           "#a8d1d5", "#b8c0df", "#e2cf9f", "#d9b5b1", "#aabfcd", "#bbd6c8",
           "#d3c0d8", "#bdc6d1", "#dfbdcb", "#d6cbaa")


def color_map(keys):
    result = {}
    for key in keys:
        if key in result:
            continue
        index = len(result)
        if index < len(PALETTE):
            result[key] = PALETTE[index]
        else:
            rgb = colorsys.hls_to_rgb((index * .61803398875) % 1, .77, .35)
            result[key] = "#" + "".join(f"{round(v * 255):02x}" for v in rgb)
    return result


def intensity(color, seconds, capacity):
    """Same hue, more saturated for more time; missing data is colored separately."""
    strength = .25 + .75 * max(0, min(1, seconds / capacity)) if capacity else 1
    values = [int(color[i:i+2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(255 + (value - 255) * strength):02x}" for value in values)


def intensity_legend(capacity_minutes):
    blocks = "".join(f'<span style="background-color:{intensity(PALETTE[0], value, 1)};'
                     'padding:1px 7px;font-size:10px">&nbsp;</span>' for value in (.1, .5, 1))
    return (f'<p style="font-size:11px;color:#64748b">同色浅→深：该应用在时段内使用较少→较多 '
            f'{blocks}（每格最多 {capacity_minutes} 分钟）</p>')
