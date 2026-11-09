#!/usr/bin/env python3
"""
Generates the animated SVG diagrams used in the README.

    python docs/diagrams/generate.py

Each diagram is a self-contained SVG: no scripts, no external fonts or images.
The motion is plain SVG/CSS animation, which GitHub plays when the file is
embedded with <img>. Colours follow the viewer's light or dark colour scheme.

A diagram is built from three things:
  box()      a labelled rounded rectangle
  arrow()    a labelled connector with "marching" dashes
  journey()  a dot that travels a route, pausing at boxes and lighting them up
"""

import math
from pathlib import Path

OUT   = Path(__file__).parent
WIDTH = 960
SPEED = 260          # dot speed, px per second
FONT  = 'ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif'
MONO  = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'

COLORS = {
    "blue":   "#3b82f6",
    "green":  "#16a34a",
    "amber":  "#d97706",
    "red":    "#dc2626",
    "purple": "#8b5cf6",
    "slate":  "#64748b",
}

BASE_CSS = f"""
svg {{ font-family: {FONT}; }}
.bg     {{ fill: #ffffff; stroke: #d1d9e0; }}
.ink    {{ fill: #1f2328; }}
.muted  {{ fill: #59636e; }}
.halo   {{ paint-order: stroke; stroke: #ffffff; stroke-width: 5px; stroke-linejoin: round; }}
.region {{ fill: #64748b; fill-opacity: .05; stroke: #8c959f; stroke-width: 1.5; stroke-dasharray: 3 5; }}
.mono   {{ font-family: {MONO}; }}
.title  {{ font-size: 14px; font-weight: 600; }}
.sub    {{ font-size: 12px; }}
.label  {{ font-size: 12px; }}
.badge  {{ font-size: 13px; font-weight: 600; }}
.edge   {{ fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.flow   {{ stroke-dasharray: 6 7; animation: march 1s linear infinite; }}
@keyframes march {{ to {{ stroke-dashoffset: -13; }} }}
@media (prefers-color-scheme: dark) {{
  .bg    {{ fill: #0d1117; stroke: #3d444d; }}
  .ink   {{ fill: #f0f6fc; }}
  .muted {{ fill: #9198a1; }}
  .halo  {{ stroke: #0d1117; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .flow {{ animation: none; }}
}}
"""


def _color_css():
    rules = []
    for name, hexv in COLORS.items():
        rules.append(f".box-{name}  {{ fill: {hexv}; fill-opacity: .12; stroke: {hexv}; stroke-width: 2; }}")
        rules.append(f".glow-{name} {{ fill: {hexv}; fill-opacity: .30; stroke: {hexv}; stroke-width: 4; opacity: 0; }}")
        rules.append(f".edge-{name} {{ stroke: {hexv}; }}")
        rules.append(f".fill-{name} {{ fill: {hexv}; }}")
    return "\n".join(rules)


class Diagram:
    def __init__(self, name, height, label):
        self.name, self.height, self.label = name, height, label
        self.back, self.front = [], []      # shapes and edges / text, drawn above the dots
        self.css, self.boxes = [], {}
        self.dots, self.glows = [], {}      # glows: (box_id, color) -> [(start, end)]
        self.timed = []                     # (svg, [(start, end)])
        self.clock = 0.0                    # running time; journeys are sequential

    # ── static pieces ────────────────────────────────────────────────────────

    def region(self, x, y, w, h, title):
        self.back.append(f'<rect class="region" x="{x}" y="{y}" width="{w}" height="{h}" rx="16"/>')
        self.back.append(f'<text class="label muted" x="{x + 14}" y="{y + 20}" '
                         f'font-weight="600" letter-spacing=".04em">{title.upper()}</text>')

    def box(self, box_id, x, y, w, h, title, sub=None, color="blue", number=None):
        self.boxes[box_id] = (x, y, w, h, color)
        cx = x + w / 2
        self.back.append(f'<rect class="box-{color}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')
        ty = y + h / 2 + (5 if sub is None else -3)
        self.front.append(f'<text class="title ink" x="{cx}" y="{ty}" text-anchor="middle">{title}</text>')
        if sub:
            self.front.append(f'<text class="sub muted" x="{cx}" y="{ty + 18}" text-anchor="middle">{sub}</text>')
        if number is not None:
            self.front.append(f'<circle class="fill-{color}" cx="{x}" cy="{y}" r="11"/>')
            self.front.append(f'<text x="{x}" y="{y + 4.5}" text-anchor="middle" font-size="12" '
                              f'font-weight="700" fill="#ffffff">{number}</text>')

    def center(self, box_id):
        x, y, w, h, _ = self.boxes[box_id]
        return x + w / 2, y + h / 2

    def arrow(self, points, color="slate", label=None, at=None, both=False, flow=True, anchor="middle"):
        d = "M" + " L".join(f"{x},{y}" for x, y in points)
        start = f' marker-start="url(#head-{color}-back)"' if both else ""
        cls = f"edge edge-{color}" + (" flow" if flow else "")
        self.back.append(f'<path class="{cls}" d="{d}" marker-end="url(#head-{color})"{start}/>')
        if label:
            lx, ly = at
            self.front.append(f'<text class="label muted halo" x="{lx}" y="{ly}" text-anchor="{anchor}">{label}</text>')

    def text(self, x, y, content, cls="label muted", anchor="middle"):
        self.front.append(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{content}</text>')

    # ── animation ────────────────────────────────────────────────────────────

    def journey(self, route, color="blue", gap=0.4):
        """
        route: list of (x, y, pause_seconds, box_to_light or None).
        The dot appears at the first point, travels the route at constant speed
        and disappears at the end. Journeys play one after another.
        """
        t0 = self.clock
        t, dist, keys = t0, 0.0, []          # keys: (time, distance)
        px, py = route[0][0], route[0][1]
        for x, y, pause, box_id in route:
            leg = math.hypot(x - px, y - py)
            dist += leg
            t += leg / SPEED
            keys.append((t, dist))
            if pause:
                if box_id:
                    if isinstance(box_id, tuple):       # (box, colour override)
                        box_id, glow_color = box_id
                    else:
                        glow_color = self.boxes[box_id][4]
                    self.glows.setdefault((box_id, glow_color), []).append((t, t + pause))
                t += pause
                keys.append((t, dist))
            px, py = x, y
        self.dots.append({"route": route, "keys": keys, "total": dist,
                          "t0": t0, "t1": t, "color": color})
        self.clock = t + gap
        return t0, t

    def show(self, svg, windows):
        """Show an SVG fragment only during the given (start, end) windows."""
        self.timed.append((svg, windows))

    def box_glow(self, box_id, start, end, color=None):
        color = color or self.boxes[box_id][4]
        self.glows.setdefault((box_id, color), []).append((start, end))

    # ── output ───────────────────────────────────────────────────────────────

    def _opacity_keyframes(self, name, windows, total, fade=0.25):
        stops = [(0.0, 0)]
        for start, end in sorted(windows):
            stops += [(max(start - fade, 0), 0), (start, 1), (end, 1), (min(end + fade, total), 0)]
        stops.append((total, 0))
        lines, last = [], -1.0
        for t, v in stops:
            pct = round(100 * t / total, 2)
            if pct <= last:                 # keep percentages strictly increasing
                pct = round(last + 0.01, 2)
            lines.append(f"{pct}% {{ opacity: {v}; }}")
            last = pct
        return f"@keyframes {name} {{ {' '.join(lines)} }}"

    def render(self, rest=1.2):
        total = round(self.clock + rest, 2)
        css, parts = [BASE_CSS, _color_css(), *self.css], []

        # Glow overlays sit just above the boxes they highlight.
        glow_svg = []
        for i, ((box_id, color), windows) in enumerate(self.glows.items()):
            x, y, w, h, _ = self.boxes[box_id]
            name = f"glow{i}"
            css.append(self._opacity_keyframes(name, windows, total))
            css.append(f".{name} {{ animation: {name} {total}s linear infinite; }}")
            glow_svg.append(f'<rect class="glow-{color} {name}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')

        timed_svg = []
        for i, (svg, windows) in enumerate(self.timed):
            name = f"show{i}"
            css.append(self._opacity_keyframes(name, windows, total))
            css.append(f".{name} {{ opacity: 0; animation: {name} {total}s linear infinite; }}")
            timed_svg.append(f'<g class="{name}">{svg}</g>')

        dot_svg = []
        for dot in self.dots:
            route, keys = dot["route"], dot["keys"]
            path = "M" + " L".join(f"{x},{y}" for x, y, *_ in route)
            frames = [(0.0, 0.0)] + keys + [(total, dot["total"])]
            times, points, last = [], [], -1.0
            for t, d in frames:
                kt = round(t / total, 5)
                if kt <= last:
                    continue
                times.append(f"{kt:g}")
                points.append(f"{(d / dot['total']):.5f}")
                last = kt
            times[-1] = "1"
            a, b = dot["t0"] / total, dot["t1"] / total
            fade = 0.15 / total
            vis_t = [0, max(a - fade, 0), a + fade, b, min(b + fade, 1), 1]
            vis_v = [0, 0, 1, 1, 0, 0]
            if a - fade <= 0:               # first journey: visible from the start
                vis_t, vis_v = [0, b, min(b + fade, 1), 1], [1, 1, 0, 0]
            dot_svg.append(
                f'<g opacity="0">'
                f'<circle class="fill-{dot["color"]}" r="13" fill-opacity=".25"/>'
                f'<circle class="fill-{dot["color"]}" r="7" stroke="#ffffff" stroke-width="2"/>'
                f'<animateMotion dur="{total}s" repeatCount="indefinite" calcMode="linear" '
                f'keyTimes="{";".join(times)}" keyPoints="{";".join(points)}" path="{path}"/>'
                f'<animate attributeName="opacity" dur="{total}s" repeatCount="indefinite" '
                f'keyTimes="{";".join(f"{t:.5f}" for t in vis_t)}" values="{";".join(map(str, vis_v))}"/>'
                f'</g>'
            )

        markers = []
        for name in COLORS:
            markers.append(
                f'<marker id="head-{name}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
                f'markerHeight="7" orient="auto"><path class="fill-{name}" d="M0,0 L10,5 L0,10 z"/></marker>'
                f'<marker id="head-{name}-back" viewBox="0 0 10 10" refX="1.5" refY="5" markerWidth="7" '
                f'markerHeight="7" orient="auto"><path class="fill-{name}" d="M10,0 L0,5 L10,10 z"/></marker>'
            )

        parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {self.height}" '
            f'width="{WIDTH}" height="{self.height}" role="img" aria-label="{self.label}">'
        )
        parts.append(f"<title>{self.label}</title>")
        parts.append("<style>" + "\n".join(css) + "</style>")
        parts.append("<defs>" + "".join(markers) + "</defs>")
        parts.append(f'<rect class="bg" x="1" y="1" width="{WIDTH - 2}" height="{self.height - 2}" rx="14"/>')
        # Dots go under the text so labels stay readable while a dot is parked on a box.
        parts += self.back + glow_svg + dot_svg + self.front + timed_svg
        parts.append("</svg>")

        target = OUT / f"{self.name}.svg"
        target.write_text("\n".join(parts) + "\n")
        print(f"{target.name:20} {total:5.1f}s loop")


# ── Diagram 1: what drift is ──────────────────────────────────────────────────

def drift():
    d = Diagram("drift", 250, "A template in Git and the resource running in AWS start out matching. "
                              "A manual console change makes them disagree: that is drift.")
    d.box("git", 60, 60, 300, 110, "", color="slate")
    d.box("aws", 600, 60, 300, 110, "", color="slate")
    d.text(210, 46, "IN GIT", "label muted")
    d.text(750, 46, "RUNNING IN AWS", "label muted")
    d.text(210, 100, "template.yaml", "title ink")
    d.text(210, 132, "InstanceType: t3.large", "title ink mono")
    d.text(750, 100, "EC2 instance", "title ink")

    in_sync, drifted, end = (0, 3.0), (3.6, 9.0), 9.0
    d.clock = end

    d.show('<text class="title ink mono" x="750" y="132" text-anchor="middle">InstanceType: t3.large</text>'
           '<path class="edge edge-green" d="M360,115 L600,115"/>'
           '<rect class="fill-green" x="425" y="99" width="110" height="32" rx="16"/>'
           '<text class="badge" x="480" y="120" text-anchor="middle" fill="#ffffff">= in sync</text>',
           [in_sync])
    d.show('<text class="title ink mono" x="750" y="132" text-anchor="middle">InstanceType: t3.xlarge</text>'
           '<path class="edge edge-red" d="M360,115 L600,115" stroke-dasharray="4 7"/>'
           '<rect class="fill-red" x="425" y="99" width="110" height="32" rx="16"/>'
           '<text class="badge" x="480" y="120" text-anchor="middle" fill="#ffffff">≠ drifted</text>',
           [drifted])
    d.show('<path class="edge edge-amber" d="M750,214 L750,180" marker-end="url(#head-amber)"/>'
           '<text class="badge fill-amber" x="750" y="234" text-anchor="middle">'
           'someone resizes it in the console</text>',
           [(3.0, end)])
    d.box_glow("aws", 3.0, 3.9, "amber")
    d.box_glow("aws", 4.2, end, "red")
    d.render(rest=0.6)


# ── Diagrams 2 and 6: a strip of numbered steps ───────────────────────────────

def strip(name, label, steps, box_w, height=170):
    d = Diagram(name, height, label)
    n   = len(steps)
    gap = (WIDTH - 48 - n * box_w) / (n - 1)
    y, h = 50, 80
    route = []
    for i, (title, sub, color) in enumerate(steps):
        x = round(24 + i * (box_w + gap), 1)
        d.box(f"s{i}", x, y, box_w, h, title, sub, color, number=i + 1)
        if i:
            d.arrow([(prev_right + 5, y + h / 2), (x - 6, y + h / 2)], "slate")
        prev_right = x + box_w
        route.append((x + box_w / 2, y + h + 22, 0.9, f"s{i}"))
    d.back.append(f'<path class="edge edge-slate" stroke-opacity=".35" '
                  f'd="M{route[0][0]},{y + h + 22} L{route[-1][0]},{y + h + 22}"/>')
    d.journey(route, "blue")
    d.render()


def flow():
    strip("flow", "The path from a manual console change to a pull request, in six steps.", [
        ("Manual change", "in the AWS Console",   "red"),
        ("CloudTrail",    "records the API call", "slate"),
        ("driftfix",      "daily run spots it",   "blue"),
        ("Bedrock LLM",   "rewrites the template", "purple"),
        ("cfn-lint",      "checks the result",    "amber"),
        ("Pull request",  "a human reviews it",   "green"),
    ], box_w=132)


def onboarding():
    strip("onboarding", "Customer onboarding in four steps.", [
        ("Enter AWS account ID", "portal builds a template",     "blue"),
        ("Deploy the template",  "trail, bucket, read-only role", "purple"),
        ("Connect GitHub",       "repository + access token",    "amber"),
        ("You are live",         "picked up by the next run",    "green"),
    ], box_w=196)


# ── Diagram 3: architecture ───────────────────────────────────────────────────

def architecture():
    d = Diagram("architecture", 510, "driftfix reads CloudTrail logs from the customer's AWS account through "
                                     "a read-only role, runs four Lambda stages in its own account, and opens "
                                     "a pull request in the customer's GitHub repository.")
    d.region(20, 30, 240, 250, "Customer AWS account")
    d.region(340, 30, 430, 460, "driftfix AWS account")

    h = 64
    d.box("stacks", 40, 70, 200, h,  "CloudFormation stacks", "someone edits one by hand", "red")
    d.box("logs",   40, 180, 200, h, "CloudTrail logs",       "in an S3 bucket",           "slate")
    d.box("cron",   360, 70, 180, h, "EventBridge",           "daily, 7 AM UTC",           "slate")
    d.box("orc",    360, 180, 180, h, "Orchestrator",         "finds manual changes",      "blue",   1)
    d.box("ddb",    570, 180, 180, h, "DynamoDB",             "status of every event",     "slate")
    d.box("sp",     360, 290, 180, h, "Stack Processor",      "one run per stack",         "blue",   2)
    d.box("llm",    570, 290, 180, h, "Amazon Bedrock",       "rewrites the template",     "purple")
    d.box("val",    360, 400, 180, h, "Validator",            "runs cfn-lint",             "blue",   3)
    d.box("pr",     570, 400, 180, h, "PR Creator",           "commits the fix",           "blue",   4)
    d.box("gh",     810, 400, 130, h, "GitHub",               "customer's repo",           "green")

    d.arrow([(140, 134), (140, 174)], label="every API call", at=(150, 160), anchor="start")
    d.arrow([(240, 212), (354, 212)], label="read-only role", at=(300, 202))
    d.arrow([(450, 134), (450, 174)])
    d.arrow([(546, 212), (564, 212)], both=True)
    d.arrow([(450, 244), (450, 284)], label="per stack", at=(460, 270), anchor="start")
    d.arrow([(546, 322), (564, 322)], both=True)
    d.arrow([(450, 354), (450, 394)])
    d.arrow([(540, 432), (564, 432)])
    d.arrow([(750, 432), (804, 432)], "green", label="PR", at=(778, 422))

    stop = lambda box, pause, light=True: (*d.center(box), pause, box if light else None)
    d.journey([
        stop("stacks", 0.7), stop("logs", 0.6), stop("orc", 0.7), stop("ddb", 0.5),
        stop("orc", 0.2, False), stop("sp", 0.6), stop("llm", 0.9), stop("sp", 0.3, False),
        stop("val", 0.7), stop("pr", 0.6), stop("gh", 1.2),
    ], "blue")
    d.render()


# ── Full system diagram (docs/system-architecture.md) ────────────────────────

def system():
    d = Diagram("system", 640, "The complete driftfix system: the customer's AWS account and GitHub "
                               "repository on the left, and on the right the driftfix account with the "
                               "four pipeline Lambdas, the services they use, monitoring, and the "
                               "onboarding portal.")
    h = 54
    rows = [64, 144, 224, 304, 384, 464, 544]
    L, A, B = 40, 310, 550                   # column x: customer, Lambdas, services
    lw, w = 190, 200

    d.region(20, 30, 230, 268, "Customer AWS account")
    d.region(290, 30, 480, 590, "driftfix AWS account")

    d.box("stacks", L, rows[0], lw, h, "CloudFormation stacks", "changed by hand",        "red")
    d.box("logs",   L, rows[1], lw, h, "CloudTrail + S3 bucket", "log of every API call", "slate")
    d.box("role",   L, rows[2], lw, h, "Read-only IAM role",     "needs an ExternalId",   "slate")
    d.box("gh",     L, rows[4], lw, h, "GitHub repository",      "templates + parameters", "green")
    d.box("eng",    L, rows[6], lw, h, "Customer engineer",      "reviews and merges",    "amber")

    d.box("cron",   A, rows[0], w, h, "EventBridge",           "daily, 7 AM UTC",          "slate")
    d.box("orc",    A, rows[1], w, h, "Orchestrator",          "finds manual changes",     "blue", 1)
    d.box("sp",     A, rows[2], w, h, "Stack Processor",       "one run per stack",        "blue", 2)
    d.box("val",    A, rows[3], w, h, "Validator",             "runs cfn-lint",            "blue", 3)
    d.box("pr",     A, rows[4], w, h, "PR Creator",            "commits the fix",          "blue", 4)
    d.box("dlq",    A, rows[5], w, h, "SQS dead-letter queue", "failed invocations",       "red")
    d.box("portal", A, rows[6], w, h, "Onboarding portal",     "App Runner: React + FastAPI", "purple")

    d.box("cw",     B, rows[0], w, h, "CloudWatch + SNS",      "dashboards, 17 alarms",    "amber")
    d.box("ddb",    B, rows[1], w, h, "DynamoDB",              "tenants + event records",  "slate")
    d.box("llm",    B, rows[2], w, h, "Amazon Bedrock",        "rewrites the template",    "purple")
    d.box("audit",  B, rows[3], w, h, "S3 audit bucket",       "validated files",          "slate")
    d.box("sec",    B, rows[4], w, h, "Secrets Manager",       "GitHub tokens",            "slate")
    d.box("health", B, rows[5], w, h, "Health Check",          "probes every 5 minutes",   "amber")
    d.box("ecr",    B, rows[6], w, h, "Amazon ECR",            "portal image",             "slate")

    mid = lambda r: rows[r] + h / 2
    # Customer side
    d.arrow([(135, rows[0] + h), (135, rows[1] - 6)])
    d.arrow([(L + lw, mid(1)), (A - 6, mid(1))], label="read logs", at=(270, mid(1) - 8))
    d.arrow([(A, mid(2) - 10), (L + lw + 6, mid(2) - 10)], label="assume", at=(270, mid(2) - 18))
    d.arrow([(A, mid(2) + 14), (270, mid(2) + 14), (270, mid(4) - 12), (L + lw + 6, mid(4) - 12)],
            label="read template", at=(262, 340), anchor="end")
    d.arrow([(A, mid(4) + 12), (L + lw + 6, mid(4) + 12)], "green", label="open PR", at=(270, mid(4) + 30))
    d.arrow([(135, rows[4] + h), (135, rows[6] - 6)], "green", label="review", at=(145, 500), anchor="start")
    d.arrow([(L + lw, mid(6)), (A - 6, mid(6))], label="sign up", at=(270, mid(6) - 8))

    # Pipeline
    for a, b in ((0, 1), (1, 2), (3, 4)):
        d.arrow([(A + w / 2, rows[a] + h), (A + w / 2, rows[b] - 6)])
    d.arrow([(A + 70, rows[2] + h), (A + 70, rows[3] - 6)], label="template", at=(A + 62, rows[3] - 10), anchor="end")
    d.arrow([(A + 130, rows[3]), (A + 130, rows[2] + h + 6)], "red", label="lint errors",
            at=(A + 138, rows[3] - 10), anchor="start")

    # Lambdas and the services they use
    for r in (1, 2, 4):
        d.arrow([(A + w + 6, mid(r)), (B - 6, mid(r))], both=True)
    d.arrow([(B, mid(5)), (A + w + 6, mid(5))])          # the health check probes the queue
    d.arrow([(A + w, mid(3)), (B - 6, mid(3))])
    d.arrow([(B, mid(6)), (A + w + 6, mid(6))])
    d.arrow([(B + w, mid(5)), (765, mid(5)), (765, mid(0)), (B + w + 6, mid(0))])

    stop = lambda box, pause, light=True: (*d.center(box), pause, box if light else None)
    cx = A + w / 2
    d.journey([
        stop("stacks", 0.6), stop("logs", 0.6), stop("orc", 0.7), stop("ddb", 0.5), stop("orc", 0.2, False),
        stop("sp", 0.6), stop("llm", 0.9), stop("sp", 0.3, False), stop("val", 0.7),
        stop("pr", 0.6), stop("gh", 0.8), stop("eng", 1.2),
    ], "blue")
    d.render()


# ── Diagram 4: the lint retry loop ────────────────────────────────────────────

def retry():
    d = Diagram("retry", 320, "When cfn-lint rejects the model's template, the Validator sends it back with "
                              "the errors and the model corrects it. The second attempt passes and goes on "
                              "to the PR Creator.")
    h = 64
    d.box("llm", 60, 40, 200, h,  "Amazon Bedrock",  "writes the template", "purple")
    d.box("sp",  60, 170, 200, h, "Stack Processor", "asks the model",      "blue")
    d.box("val", 380, 170, 200, h, "Validator",      "runs cfn-lint",       "amber")
    d.box("pr",  700, 170, 200, h, "PR Creator",     "opens the pull request", "green")

    d.arrow([(160, 164), (160, 110)], both=True)
    d.arrow([(266, 188), (374, 188)], label="template", at=(320, 178))
    d.arrow([(374, 216), (266, 216)], "red", label="lint errors", at=(320, 236))
    d.arrow([(586, 202), (694, 202)], "green", label="passes", at=(640, 192))
    d.text(480, 300, "At most 3 retries. After that the events are marked failed and an alarm fires.")

    sp, llm, val, pr = (d.center(b) for b in ("sp", "llm", "val", "pr"))
    _, t1 = d.journey([
        (*sp, 0.4, "sp"), (*llm, 0.8, "llm"), (*sp, 0.3, None), (*val, 1.3, ("val", "red")),
    ], "blue", gap=0)
    bad = (t1 - 1.3, t1)
    _, t2 = d.journey([
        (*val, 0, None), (*sp, 0.4, "sp"), (*llm, 0.8, "llm"), (*sp, 0.3, None), (*val, 1.3, ("val", "green")),
    ], "red", gap=0)
    good = (t2 - 1.3, t2)
    d.journey([(*val, 0, None), (*pr, 1.2, "pr")], "green")

    pill = ('<rect class="fill-{c}" x="400" y="252" width="160" height="28" rx="14"/>'
            '<text class="badge" x="480" y="271" text-anchor="middle" fill="#ffffff">{t}</text>')
    d.show(pill.format(c="red", t="attempt 1: 2 errors"), [bad])
    d.show(pill.format(c="green", t="attempt 2: clean"), [good])
    d.render()


# ── Diagram 5: the life of one event ──────────────────────────────────────────

def lifecycle():
    d = Diagram("lifecycle", 380, "Every CloudTrail event is tracked from queued to one of three outcomes: "
                                  "pr_opened, no_change, or failed. Failed events are retried on the next "
                                  "daily run, at most three times.")
    h = 60
    d.box("queued", 100, 150, 180, h, "queued",    "picked up by a run",       "blue")
    d.box("opened", 660, 40, 230, h, "pr_opened", "done: a PR carries the fix", "green")
    d.box("none",   660, 150, 230, h, "no_change", "done: nothing to fix",    "slate")
    d.box("failed", 660, 260, 230, h, "failed",   "a stage reported an error", "red")

    d.back.append('<circle class="fill-slate" cx="30" cy="180" r="6"/>')
    d.arrow([(40, 180), (94, 180)])
    d.text(18, 162, "new event", anchor="start")
    d.arrow([(280, 180), (654, 180)], label="model changed nothing", at=(540, 170))
    d.arrow([(440, 180), (440, 70), (654, 70)], "green", label="pull request created", at=(548, 60))
    d.arrow([(440, 180), (440, 290), (654, 290)], "red", label="lint, GitHub or model error", at=(548, 280))
    d.arrow([(775, 320), (775, 350), (190, 350), (190, 216)], "red",
            label="retried on the next daily run, max 3 attempts", at=(482, 341))

    q, opened, none, failed = (d.center(b) for b in ("queued", "opened", "none", "failed"))
    start = (30, 180, 0, None)
    d.journey([start, (*q, 0.6, "queued"), (440, 180, 0, None), (440, 70, 0, None),
               (*opened, 1.1, "opened")], "green")
    d.journey([start, (*q, 0.6, "queued"), (*none, 1.1, "none")], "slate")
    d.journey([start, (*q, 0.6, "queued"), (440, 180, 0, None), (440, 290, 0, None),
               (*failed, 0.9, "failed"), (775, 350, 0, None), (190, 350, 0, None),
               (*q, 0.7, "queued"), (440, 180, 0, None), (440, 70, 0, None),
               (*opened, 1.2, "opened")], "red")
    d.render()


# ── Charts: load test results ─────────────────────────────────────────────────
# Numbers come from results/report.md (section 3, throughput; section 4a, Bedrock
# latency). They are drawn once on load rather than looped, so the final state
# is what a reader sees after scrolling to them.

CHART_CSS = """
.s1 { fill: #2a78d6; stroke: #2a78d6; }
.s2 { fill: #eb6834; stroke: #eb6834; }
.grid { stroke: #e3e6ea; stroke-width: 1; }
.axis { stroke: #8c959f; stroke-width: 1; }
.num  { font-variant-numeric: tabular-nums; }
.hollow { fill: #ffffff; stroke-width: 2; }
.line { fill: none !important; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.dot  { stroke: #ffffff; stroke-width: 2; }
.grow { transform-box: fill-box; transform-origin: left center; animation: grow .9s cubic-bezier(.2,.7,.2,1) both; }
.draw { animation: draw 1.4s ease-out both; }
.fade { animation: fade .5s ease-out both; }
@keyframes grow { from { transform: scaleX(0); } }
@keyframes draw { to { stroke-dashoffset: 0; } }
@keyframes fade { from { opacity: 0; } }
@media (prefers-color-scheme: dark) {
  .s1 { fill: #3987e5; stroke: #3987e5; }
  .s2 { fill: #d95926; stroke: #d95926; }
  .grid { stroke: #262c34; }
  .axis { stroke: #6e7681; }
  .hollow { fill: #0d1117; }
  .dot { stroke: #0d1117; }
}
@media (prefers-reduced-motion: reduce) {
  .grow, .draw, .fade { animation: none; }
  .draw { stroke-dashoffset: 0 !important; }
}
"""


def write_chart(name, height, label, body):
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img" aria-label="{label}">',
        f"<title>{label}</title>",
        "<style>" + BASE_CSS + CHART_CSS + "</style>",
        f'<rect class="bg" x="1" y="1" width="{WIDTH - 2}" height="{height - 2}" rx="14"/>',
        *body,
        "</svg>",
    ]
    (OUT / f"{name}.svg").write_text("\n".join(svg) + "\n")
    print(f"{name + '.svg':20} chart")


def throughput():
    # (scenario, load, stacks per minute), ordered by load.
    rows = [
        ("T1 Functional",  "1 stack",                 15.9),
        ("T2 Scaling",     "3 stacks",                19.2),
        ("T5 Security",    "6 stacks",                31.0),
        ("T3 Performance", "10 stacks",               39.5),
        ("T4 Failure",     "10 stacks + 1 bad tenant", 36.6),
    ]
    x0, x1, top, row_h, bar_h, vmax = 250, 880, 78, 44, 22, 45
    scale  = (x1 - x0) / vmax
    bottom = top + row_h * len(rows)
    body = [
        '<text class="title ink" x="32" y="38">Throughput grows with load</text>',
        '<text class="sub muted" x="32" y="58">Stacks processed per minute, by number of drifted stacks</text>',
    ]
    for tick in range(0, vmax + 1, 10):
        x = x0 + tick * scale
        body.append(f'<line class="grid" x1="{x}" y1="{top}" x2="{x}" y2="{bottom}"/>')
        body.append(f'<text class="label muted num" x="{x}" y="{bottom + 20}" text-anchor="middle">{tick}</text>')
    for i, (scenario, load, value) in enumerate(rows):
        cy = top + row_h * i + row_h / 2
        w  = round(value * scale, 1)
        y  = cy - bar_h / 2
        delay = f"{0.15 * i:.2f}s"
        body.append(f'<text class="title ink" x="{x0 - 16}" y="{cy - 2}" text-anchor="end">{load}</text>')
        body.append(f'<text class="sub muted" x="{x0 - 16}" y="{cy + 14}" text-anchor="end">{scenario}</text>')
        # Square at the baseline, rounded at the data end.
        body.append(f'<path class="s1 grow" stroke="none" style="animation-delay:{delay}" '
                    f'd="M{x0},{y} h{w - 4} a4,4 0 0 1 4,4 v{bar_h - 8} a4,4 0 0 1 -4,4 h{-(w - 4)} z"/>')
        body.append(f'<text class="title ink num fade" style="animation-delay:{0.15 * i + 0.7:.2f}s" '
                    f'x="{x0 + w + 10}" y="{cy + 5}">{value}</text>')
    body.append(f'<line class="axis" x1="{x0}" y1="{top}" x2="{x0}" y2="{bottom}"/>')
    write_chart("throughput", bottom + 36,
                "Throughput rises from 15.9 stacks per minute with 1 drifted stack to 39.5 with 10.", body)


def latency():
    # (stacks, P50 seconds, P99 seconds). The second 10-stack run (T4) is drawn
    # as hollow markers so both measurements at that load are visible.
    runs   = [(1, 1.71, 1.71), (3, 4.61, 7.41), (6, 4.34, 9.66), (10, 5.11, 13.26)]
    second = (10, 5.28, 14.86)
    x0, x1, top, bottom, ymax = 80, 760, 96, 330, 16
    fx = lambda stacks: round(x0 + (stacks - 1) / 9 * (x1 - x0), 1)
    fy = lambda secs: round(bottom - secs / ymax * (bottom - top), 1)

    body = [
        '<text class="title ink" x="32" y="38">The typical call stays fast. The slowest calls do not.</text>',
        '<text class="sub muted" x="32" y="58">Amazon Bedrock response time in seconds, by number of stacks processed in parallel</text>',
        # Legend
        '<line class="line s1" x1="32" y1="78" x2="52" y2="78"/><circle class="s1 dot" cx="42" cy="78" r="4.5"/>',
        '<text class="label ink" x="60" y="82">P50, the typical call</text>',
        '<line class="line s2" x1="212" y1="78" x2="232" y2="78"/><circle class="s2 dot" cx="222" cy="78" r="4.5"/>',
        '<text class="label ink" x="240" y="82">P99, the slowest 1%</text>',
        '<circle class="s2 hollow" cx="400" cy="78" r="4.5"/><circle class="s1 hollow" cx="413" cy="78" r="4.5"/>',
        '<text class="label ink" x="425" y="82">second run at 10 stacks</text>',
    ]
    for tick in range(0, ymax + 1, 4):
        y = fy(tick)
        body.append(f'<line class="{"axis" if tick == 0 else "grid"}" x1="{x0}" y1="{y}" x2="{x1}" y2="{y}"/>')
        body.append(f'<text class="label muted num" x="{x0 - 12}" y="{y + 4}" text-anchor="end">{tick} s</text>')
    for stacks in (1, 3, 6, 10):
        body.append(f'<text class="label muted num" x="{fx(stacks)}" y="{bottom + 20}" text-anchor="middle">'
                    f'{stacks} stack{"s" if stacks > 1 else ""}</text>')

    for cls, col in (("s1", 1), ("s2", 2)):
        pts    = [(fx(r[0]), fy(r[col])) for r in runs]
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
        d      = "M" + " L".join(f"{x},{y}" for x, y in pts)
        body.append(f'<path class="line {cls} draw" d="{d}" '
                    f'stroke-dasharray="{length:.0f}" stroke-dashoffset="{length:.0f}"/>')
        for i, (x, y) in enumerate(pts):
            body.append(f'<circle class="{cls} dot fade" style="animation-delay:{0.35 * i:.2f}s" '
                        f'cx="{x}" cy="{y}" r="5"/>')
        body.append(f'<circle class="{cls} hollow fade" style="animation-delay:1.4s" '
                    f'cx="{fx(second[0])}" cy="{fy(second[col])}" r="5"/>')

    # Direct labels: the shared start, each line's end, and the second run's P99.
    end = fx(10) + 14
    for x, y, text, anchor in (
        (fx(1) + 12, fy(1.71) + 16, "1.7 s for both", "start"),
        (end, fy(13.26) + 5, "P99 13.3 s", "start"),
        (end, fy(14.86) + 1, "14.9 s on the second run", "start"),
        (end, fy(5.11) + 12, "P50 5.1 s", "start"),
    ):
        body.append(f'<text class="label ink num fade" style="animation-delay:1.5s" '
                    f'x="{x}" y="{y}" text-anchor="{anchor}">{text}</text>')
    write_chart("latency", bottom + 40,
                "Bedrock P50 latency stays near 5 seconds from 3 to 10 parallel stacks, "
                "while P99 latency climbs from 1.7 seconds at 1 stack to about 14 seconds at 10.", body)


if __name__ == "__main__":
    for build in (drift, flow, architecture, system, retry, lifecycle, onboarding, throughput, latency):
        build()
