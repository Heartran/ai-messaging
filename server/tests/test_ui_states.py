"""The chat window's states (design §10.10), pinned where a refactor would
silently drop them: the page is one static file and no browser runs in CI,
so these read the source for the promises the design makes."""

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "aim_server" / "static" / "ui.html"


@pytest.fixture(scope="module")
def page() -> str:
    return UI.read_text("utf-8")


def test_skeleton_is_a_slow_left_to_right_shimmer(page):
    keyframes = re.search(r"@keyframes shimmer \{(.*?)\n\}", page, re.S)
    assert keyframes, "the skeleton must be animated, never a static grey box"
    body = keyframes.group(1)
    # From a positive to a negative background position: the band travels
    # left to right. A pulse would animate opacity instead.
    assert "from { background-position: 200% 0; }" in body
    assert "to   { background-position: -200% 0; }" in body
    assert "opacity" not in body
    duration = re.search(r"animation: shimmer ([\d.]+)s linear infinite", page)
    assert duration and float(duration.group(1)) >= 1.5, "slow and constant"


def test_motion_is_dropped_for_people_who_asked(page):
    reduced = page.split("@media (prefers-reduced-motion: reduce)", 1)
    assert len(reduced) == 2
    assert ".skel, .spin { animation: none; }" in reduced[1][:200]


def test_a_late_skeleton_hands_over_to_words_and_a_way_out(page):
    steps = re.search(r"const SLOW_STEPS = \[(.*?)\];", page, re.S)
    assert steps, "no fallback for a slow network: the skeleton would stare forever"
    times = [int(t) for t in re.findall(r"\[(\d+),", steps.group(1))]
    assert times[0] <= 3000, "past ~3 s the skeleton stops being honest"
    assert times[-1] <= 10000, "past ~10 s a loop alone irritates: offer a retry"
    assert '"Try again"' in page


def test_each_section_says_its_own_failure(page):
    # The transcript, the chat list and the participants line fail apart,
    # in place, each with a retry: no page-wide error for one read.
    for marker in ("function renderThreadError", "Couldn't load the chats",
                   "function renderParticipantsError"):
        assert marker in page
    assert "Promise.allSettled" in page


def test_a_failed_send_is_said_under_the_composer_and_keeps_the_text(page):
    send = page.split("async function sendMessage()", 1)[1].split("\nfunction ", 1)[0]
    assert "setComposerStatus(" in send
    assert "Your text is still here" in send
    assert "toast(error.message" not in send, "a toast is missed if you look away"


def test_the_overview_reads_stats_and_keeps_its_states(page):
    # §10.11: one read, a first-load skeleton, the last numbers kept when a
    # refresh fails, and an empty state in every panel.
    loader = page.split("async function loadStats()", 1)[1].split("\nasync function ", 1)[0]
    assert 'api("/stats")' in loader
    assert "overviewSkeleton" in loader and "SLOW_STEPS" in loader
    assert "Couldn't load the overview" in loader
    assert "Couldn't refresh" in page
    for empty in ("No chats yet", "Nothing waiting", "No participants yet", "No memories yet"):
        assert empty in page


def test_overview_warm_colour_never_stands_alone(page):
    # Problems carry a non-colour cue: every warm chip starts with ▲.
    chips = re.findall(r'mk\("span", "chip-warn", ([^)]*)\)', page)
    assert chips
    for expr in chips:
        assert "▲" in expr, expr


def test_the_memory_browser_only_reads(page):
    # §10.11: the memory layer gets a face, not a second write path.
    browser = page.split("memory browser (§10.11)", 1)[1].split("The same /stats answer", 1)[0]
    assert 'api("/memories?"' in browser
    assert "method:" not in browser and "post(" not in browser
    assert "Couldn't load the memories" in browser and "showLoading(" in browser


def test_selection_is_a_fill_not_a_left_edge(page):
    for rule in (".focus-item.on {", ".chat-item.on {"):
        block = page.split(rule, 1)[1].split("}", 1)[0]
        assert "border-left" not in block
        assert "box-shadow: inset 0 0 0 1px" in block
