"""Render Bench 1's comparison figure as a self-contained SVG from scored turns, without inference."""

from __future__ import annotations

import argparse
import json
import math
from html import escape
from pathlib import Path
from statistics import median

ARMS = {"orb": "Orb", "tt-handoff": "TauriTavern handoff Profiles", "tt-single": "TauriTavern single Profile"}
SHORT = {"orb": "Orb", "tt-handoff": "TT handoff", "tt-single": "TT single"}
SHAPES = {"orb": "circle", "tt-handoff": "square", "tt-single": "triangle"}
PHASES = ("turn 1", "later turns")
WIDTH = 960
STYLE = """
svg { --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #6f6d68; --grid: #e1e0d9; --axis: #c3c2b7;
  --s-orb: #2a78d6; --s-tt-handoff: #eb6834; --s-tt-single: #1baf7a; }
@media (prefers-color-scheme: dark) {
  svg { --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #9a988f; --grid: #2c2c2a; --axis: #383835;
    --s-orb: #3987e5; --s-tt-handoff: #d95926; --s-tt-single: #199e70; }
}
text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--ink); }
.title { font-size: 18px; font-weight: 600; }
.sub { font-size: 12.5px; fill: var(--ink-2); }
.panel { font-size: 13px; font-weight: 600; }
.note { font-size: 11.5px; fill: var(--ink-2); }
.tick { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.label { font-size: 11.5px; font-weight: 600; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { opacity: 0.4; }
.median { stroke: var(--surface); stroke-width: 2; }
.hollow { fill: var(--surface); stroke-width: 2; }
"""


def size_of(row):
    return int(row["fixture"].removeprefix("bellwick-"))


def phase(row):
    return "turn 1" if row["turn"] == 1 else "later turns"


def known(values):
    return [value for value in values if value is not None]


def nice_max(value):
    magnitude = 10 ** math.floor(math.log10(value / 5))
    step = next(factor * magnitude for factor in (1, 2, 2.5, 5, 10) if factor * magnitude * 5 >= value)
    return math.ceil(value / step) * step, step


def compact(value):
    if value >= 1000:
        return f"{value / 1000:g}k"
    return f"{value:g}"


def shape(arm, x, y, r, css="", title=""):
    fill = f"var(--s-{arm})"
    hollow = "hollow" in css
    paint = f'fill="var(--surface)" stroke="{fill}"' if hollow else f'fill="{fill}"'
    tip = f"<title>{escape(title)}</title>" if title else ""
    if SHAPES[arm] == "circle":
        return f'<circle class="{css}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" {paint}>{tip}</circle>'
    if SHAPES[arm] == "square":
        side = r * 1.8
        return (
            f'<rect class="{css}" x="{x - side / 2:.1f}" y="{y - side / 2:.1f}" width="{side:.1f}" height="{side:.1f}" '
            f'rx="1" {paint}>{tip}</rect>'
        )
    h = r * 1.15
    points = f"{x:.1f},{y - h * 1.15:.1f} {x + h:.1f},{y + h * 0.85:.1f} {x - h:.1f},{y + h * 0.85:.1f}"
    return f'<polygon class="{css}" points="{points}" stroke-linejoin="round" {paint}>{tip}</polygon>'


class Panel:
    """One plot area with a linear y axis from zero and a linear or categorical x axis."""

    def __init__(self, x, y, width, height, title, y_max, y_format=compact, x_max=None, categories=None):
        self.left, self.top, self.width, self.height = x + 44, y + 26, width - 52, height - 26 - 30
        self.title, self.x0 = title, x
        self.y_max, self.y_step = nice_max(y_max)
        self.y_format, self.categories = y_format, categories
        if x_max is not None:
            self.x_max, self.x_step = nice_max(x_max)

    def px(self, value):
        if self.categories:
            slot = self.width / len(self.categories)
            return self.left + slot * (self.categories.index(value) + 0.5)
        return self.left + self.width * value / self.x_max

    def py(self, value):
        return self.top + self.height * (1 - value / self.y_max)

    def frame(self, x_label):
        out = [f'<text class="panel" x="{self.x0}" y="{self.top - 12}">{escape(self.title)}</text>']
        ticks = int(round(self.y_max / self.y_step))
        for index in range(ticks + 1):
            value = index * self.y_step
            y = self.py(value)
            out.append(
                f'<line class="{"axis" if index == 0 else "grid"}" x1="{self.left}" x2="{self.left + self.width}" y1="{y:.1f}" y2="{y:.1f}"/>'
            )
            out.append(
                f'<text class="tick" x="{self.left - 8}" y="{y + 4:.1f}" text-anchor="end">{self.y_format(value)}</text>'
            )
        bottom = self.top + self.height
        if self.categories:
            marks = [(self.px(value), compact(value)) for value in self.categories]
        else:
            marks = [
                (self.px(i * self.x_step), compact(i * self.x_step)) for i in range(int(round(self.x_max / self.x_step)) + 1)
            ]
        for x, text in marks:
            out.append(f'<text class="tick" x="{x:.1f}" y="{bottom + 16}" text-anchor="middle">{text}</text>')
        out.append(
            f'<text class="tick" x="{self.left + self.width:.1f}" y="{bottom + 30}" text-anchor="end">{escape(x_label)}</text>'
        )
        return out


def groups(rows):
    """Grouped medians per arm, phase and starting size: (x, y) pairs for the median lines."""
    out = {}
    for arm in ARMS:
        for name in PHASES:
            for size in sorted({size_of(row) for row in rows}):
                group = [r for r in rows if r["arm"] == arm and phase(r) == name and size_of(r) == size]
                out[arm, name, size] = group
    return out


def scatter(panel, rows, arm, y_key, qualified_only):
    out = []
    for row in rows:
        if row["arm"] != arm or (qualified_only and not row["qualified"]):
            continue
        x, y = row["actual_prompt_tokens"], row[y_key]
        if x is None or y is None:
            continue
        tip = f"{ARMS[arm]} · {row['block']} turn {row['turn']} · context {x:,} · {y_key.replace('_', ' ')} {y:,.1f}"
        out.append(shape(arm, panel.px(x), panel.py(y), 2.6, "dot", tip))
    return out


def median_line(panel, grouped, arm, name, y_key, qualified_only, label=False):
    points = []
    for (group_arm, group_phase, size), group in grouped.items():
        if group_arm != arm or group_phase != name:
            continue
        chosen = [r for r in group if r["qualified"]] if qualified_only else group
        pairs = [
            (r["actual_prompt_tokens"], r[y_key])
            for r in chosen
            if r["actual_prompt_tokens"] is not None and r[y_key] is not None
        ]
        if pairs:
            points.append((size, median(x for x, _ in pairs), median(y for _, y in pairs), len(pairs)))
    if not points:
        return []
    path = " ".join(f"{'M' if i == 0 else 'L'}{panel.px(x):.1f},{panel.py(y):.1f}" for i, (_, x, y, _) in enumerate(points))
    out = [f'<path class="line" d="{path}" stroke="var(--s-{arm})"/>']
    for size, x, y, count in points:
        tip = f"{ARMS[arm]} · {name} · start {size:,} · median of {count}: context {x:,.0f}, {y:,.1f}"
        out.append(shape(arm, panel.px(x), panel.py(y), 4.5, "median", tip))
    if label:
        _, x, y, _ = points[-1]
        out.append(f'<text class="label" x="{panel.px(x) + 9:.1f}" y="{panel.py(y) + 4:.1f}">{SHORT[arm]}</text>')
    return out


def category_series(panel, rows, arm, sizes, value, offset, hollow=False, line=True, unit=""):
    out, points = [], []
    for size in sizes:
        group = [r for r in rows if r["arm"] == arm and size_of(r) == size]
        result = value(group)
        if result is None:
            continue
        points.append((panel.px(size) + offset, panel.py(result[0]), size, result))
    if line and points:
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y, _, _) in enumerate(points))
        out.append(f'<path class="line" d="{path}" stroke="var(--s-{arm})"/>')
    for x, y, size, (_, detail) in points:
        out.append(shape(arm, x, y, 4.5, "hollow" if hollow else "median", f"{ARMS[arm]} · start {size:,} · {detail}"))
    return out


def render(rows):
    grouped = groups(rows)
    sizes = sorted({size_of(row) for row in rows})
    context_max = max(known(r["actual_prompt_tokens"] for r in rows))
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{{height}}" viewBox="0 0 {WIDTH} {{height}}" role="img" '
        'aria-labelledby="t d">',
        f"<style>{STYLE}</style>",
        '<title id="t">Bench 1: turn time and uncached input against context size</title>',
        '<desc id="d">Per-turn wall time and uncached input tokens against the largest prompt in the turn, for Orb and two '
        "TauriTavern configurations, with turn 1 after a cold server start apart from turns 2 to 10, then completion rates "
        "and output lengths by starting history. Every value is in turns.csv.gz and summary.csv beside this figure.</desc>",
        '<rect width="100%" height="100%" rx="8" fill="var(--surface)"/>',
        '<text class="title" x="24" y="36">Turn time and uncached input against actual context size</text>',
        f'<text class="sub" x="24" y="56">Bench 1 · {len(rows)} attempted turns · Gemma 4 26B-A4B on llama.cpp, one RTX 3090 · '
        "starting histories " + ", ".join(compact(s) for s in sizes) + " tokens, 3 blocks of 10 turns each</text>",
    ]
    legend_x = 24
    for arm, name in ARMS.items():
        out.append(shape(arm, legend_x + 5, 79, 4.5, "median"))
        out.append(f'<text class="note" x="{legend_x + 16}" y="83">{escape(name)}</text>')
        legend_x += 30 + 6.6 * len(name)
    out.append(
        f'<text class="note" x="{legend_x + 8}" y="83">Small marks: one turn · large marks and lines: median per starting history</text>'
    )

    half = (WIDTH - 48 - 40) / 2
    top = 104
    later_wall = known(r["native_wall_seconds"] for r in rows if r["qualified"])
    wall_max = max(later_wall)
    for row_index, (y_key, qualified_only, heading, y_format) in enumerate(
        (
            ("native_wall_seconds", True, "Wall seconds per turn, qualified turns", lambda v: f"{v:g}"),
            ("uncached_tokens", False, "Uncached input tokens per turn, all attempts", compact),
        )
    ):
        for column, name in enumerate(PHASES):
            values = known(r[y_key] for r in rows if phase(r) == name and (r["qualified"] or not qualified_only))
            y_max = wall_max if y_key == "native_wall_seconds" else max(values)
            title = f"{heading} · {'turn 1 (cold server)' if name == 'turn 1' else 'turns 2–10'}"
            panel = Panel(24 + column * (half + 40), top, half, 250, title, y_max, y_format, x_max=context_max)
            out.extend(panel.frame("largest prompt in the turn, tokens →"))
            for arm in ARMS:
                out.extend(scatter(panel, [r for r in rows if phase(r) == name], arm, y_key, qualified_only))
            for arm in ARMS:
                out.extend(median_line(panel, grouped, arm, name, y_key, qualified_only, label=name == "later turns"))
        top += 270
        if row_index == 0:
            out.append(
                f'<text class="note" x="24" y="{top - 6}">Both wall-time panels share one scale. '
                "The uncached-input panels have their own scales.</text>"
            )
            top += 12

    third = (WIDTH - 48 - 2 * 36) / 3
    top += 8

    def completion(group):
        if not group:
            return None
        qualified = sum(r["qualified"] for r in group)
        return 100 * qualified / len(group), f"qualified {qualified}/{len(group)}"

    def native(group):
        if not group:
            return None
        failed = sum((not r["complete"]) if r["arm"] == "orb" else r.get("status") != "completed" for r in group)
        return 100 * (len(group) - failed) / len(group), f"native run completed {len(group) - failed}/{len(group)}"

    def words(group):
        values = [r["prose_words"] for r in group if r["qualified"] and r["prose_words"] is not None]
        return (
            (median(values), f"median saved reply {median(values):,.0f} words over {len(values)} qualified turns")
            if values
            else None
        )

    def generated(group):
        values = known(r["generated_tokens"] for r in group)
        return (
            (median(values), f"median {median(values):,.0f} generated tokens over {len(values)} attempts") if values else None
        )

    def peak(value):
        found = [value([r for r in rows if r["arm"] == arm and size_of(r) == s]) for arm in ARMS for s in sizes]
        return max(result[0] for result in found if result is not None)

    word_max, gen_max = peak(words), peak(generated)
    offsets = {"orb": -9, "tt-handoff": 0, "tt-single": 9}
    specs = (
        ("Turns meeting the task contract, %", 100, lambda v: f"{v:g}", completion),
        ("Saved reply words, median", word_max, compact, words),
        ("Generated tokens per turn, median", gen_max, compact, generated),
    )
    for column, (title, y_max, y_format, value) in enumerate(specs):
        panel = Panel(24 + column * (third + 36), top, third, 210, title, y_max, y_format, categories=sizes)
        out.extend(panel.frame("starting history, tokens →"))
        for arm in ARMS:
            if value is completion:
                out.extend(category_series(panel, rows, arm, sizes, native, offsets[arm], hollow=True, line=False))
            out.extend(category_series(panel, rows, arm, sizes, value, offsets[arm]))
    top += 222
    notes = [
        "Contract panel: filled marks met the shared task contract; hollow marks are runs the application itself completed.",
        "The bottom row pools all 30 attempts per arm and starting history. Generated tokens include tool-call JSON.",
        "Wall time runs from the trigger to the persisted reply. Each starting history is one frozen chat, so these are descriptive",
        "medians for this fixture and these configurations, without intervals. Every value is in turns.csv.gz and summary.csv.",
    ]
    for line in notes:
        out.append(f'<text class="note" x="24" y="{top}">{escape(line)}</text>')
        top += 17
    height = top + 8
    out.append("</svg>")
    return "\n".join(out).replace("{height}", str(int(height)))


def section(rows, figure_name="figure.svg"):
    """The report's figure section: what the figure encodes and what it shows, stated from the data."""
    grouped = groups(rows)
    largest = max(size_of(row) for row in rows)

    def med(arm, name, key, size=largest, qualified=False):
        group = [r for r in grouped[arm, name, size] if r["qualified"] or not qualified]
        values = known(r[key] for r in group)
        return median(values) if values else None

    def overall(arm, key, qualified=True):
        values = known(r[key] for r in rows if r["arm"] == arm and (r["qualified"] or not qualified))
        return median(values) if values else None

    smallest = min(size_of(row) for row in rows)
    later = ", ".join(
        f"{SHORT[arm]} {med(arm, 'later turns', 'native_wall_seconds', smallest, True):.1f} → "
        f"{med(arm, 'later turns', 'native_wall_seconds', largest, True):.1f} s"
        for arm in ARMS
    )
    uncached = ", ".join(
        f"{SHORT[arm]} {med(arm, 'later turns', 'uncached_tokens'):,.0f} of {med(arm, 'later turns', 'actual_prompt_tokens'):,.0f}"
        for arm in ARMS
    )
    cold = ", ".join(
        f"{SHORT[arm]} {med(arm, 'turn 1', 'uncached_tokens'):,.0f} of {med(arm, 'turn 1', 'actual_prompt_tokens'):,.0f}"
        for arm in ARMS
    )
    lengths = "; ".join(
        f"{SHORT[arm]} {overall(arm, 'prose_words'):,.0f} words, {overall(arm, 'generated_tokens', False):,.0f} tokens"
        for arm in ARMS
    )
    return [
        "## Figure",
        "",
        f"![Bench 1: turn time and uncached input against actual context size]({figure_name})",
        "",
        "Each small mark is one turn, placed at the largest prompt it sent; large marks and lines are medians per starting history. "
        "Wall time uses qualified turns; uncached input and generated tokens use all attempts. The bottom row pools every turn of a size.",
        "",
        f"- Turn 1 after a cold server start, {largest:,} start, median uncached input against median largest prompt: {cold}. "
        "Uncached input sums every call in the turn, so it can exceed the largest single prompt.",
        f"- Turns 2–10, {largest:,} start, the same measure: {uncached}.",
        f"- Median wall time over turns 2–10 from the {smallest:,} to the {largest:,} start: {later}.",
        f"- Median saved reply (qualified turns) and generated tokens (all attempts): {lengths}.",
        "",
        "The arms differ in call count, output length and tool behavior as well as cache reuse; the figure does not separate these causes.",
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.turns.read_text())))
