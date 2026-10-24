"""The terminal side of the watcher's board (design 24.5).

A ``Screen`` has three methods: ``size()`` (columns, rows), ``draw(lines)`` (the
lines ``board.render`` returns, each a list of ``(text, style)`` spans) and
``getch(timeout_ms)`` (a key name, a click, or None when nothing was pressed in that
time), plus ``close()`` and a ``colours`` attribute. Keys come back as the character
typed (``"1"``, ``"a"``, ``"?"``) or one of ``UP``, ``DOWN``, ``LEFT``, ``RIGHT``,
``ENTER``, ``ESC``, ``BACKSPACE``, ``PAGEUP``, ``PAGEDOWN`` and ``RESIZE`` (the terminal
changed size: read ``size()`` again and redraw); a left click as ``("click", row, col)``
(design 25.1; the screen asks for mouse events with ``mousemask``). Clicks curses does not
decode itself, in SGR or X10 encoding, are parsed from the raw bytes, and any other escape
sequence curses passes on is dropped whole, never read as keys (25.8).

- ``CursesScreen`` wraps ``curses``. ``open_screen`` starts it on the process's
  terminal and returns None when that cannot be done (no ``curses`` module, an
  unknown terminal), and the watcher then prints its lines as before. ``curses``
  is imported only there, so every other command works where it is missing.
  Colours are used only when the terminal has them and ``NO_COLOR`` is not set;
  without them, ``bold`` and ``reverse`` still mark the blocked state and the
  selected row. Ctrl-C keeps its meaning (cbreak, not raw mode).
- ``FakeScreen`` is for tests: it records every draw and replays a script of keys
  (and ``("click", row, col)``, ``("resize", w, h)``, ``("wait", seconds)`` or a callable
  run between keys),
  advancing a fake clock while it waits, and raises KeyboardInterrupt, as Ctrl-C
  would, once the script and its idle polls are spent.

``zoom`` is the one herdr command here: ``pane zoom --current --toggle`` on the strip's
own pane (``HERDR_PANE_ID``), else on the watch pane the state file records, and nothing
when neither is known (design 25.1). Nothing here talks to an agent.
"""

from __future__ import annotations

import locale
import os
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from herdr_fleet.board import cut, width_of

try:  # typing.Protocol is in 3.8; nothing depends on it at run time
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

Span = Tuple[str, str]
Line = List[Span]

UP, DOWN, LEFT, RIGHT, ENTER, RESIZE = "up", "down", "left", "right", "enter", "resize"
ESC, BACKSPACE, PAGEUP, PAGEDOWN = "esc", "backspace", "pageup", "pagedown"
CLICK = "click"
ESC_DELAY_MS = 25   # how long curses waits after Esc for the rest of an escape sequence
ESC_FOLLOW_MS = 50  # design 25.8: how long the board waits for the next byte of a sequence curses passed on
SEQUENCE_MAX = 32   # bytes of a sequence read before it is given up and dropped
BUTTON_BITS = 3 | 32 | 64  # in a mouse report: the button (0 is the left), motion, the wheel
PAIRS = {"red": 1, "green": 2, "yellow": 3}


class Screen(Protocol):  # pragma: no cover - a description, not code
    colours: bool

    def size(self) -> Tuple[int, int]:
        ...

    def draw(self, lines: Sequence[Line]) -> None:
        ...

    def getch(self, timeout_ms: int):  # a key name, ("click", row, col), or None
        ...

    def close(self) -> None:
        ...


def sgr_click(params: str, final: str):
    """``ESC [ < b ; x ; y M`` (press) or ``… m`` (release), 1-based: a press of the left
    button, without motion or wheel bits, is ``("click", row, col)``; anything else None."""
    parts = params.split(";")
    if final != "M" or len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    button, x, y = (int(p) for p in parts)
    if button & BUTTON_BITS or x < 1 or y < 1:
        return None
    return (CLICK, y - 1, x - 1)


def colours_wanted(env: Mapping[str, str]) -> bool:
    """Colours unless ``NO_COLOR`` is set to anything (https://no-color.org)."""
    return not env.get("NO_COLOR")


class CursesScreen:
    """A Screen on the real terminal."""

    def __init__(self, curses, stdscr, colours: bool):
        self.curses = curses
        self.stdscr = stdscr
        self.colours = colours
        self.closed = False

    def size(self) -> Tuple[int, int]:
        rows, cols = self.stdscr.getmaxyx()
        return cols, rows

    def attr(self, style: str) -> int:
        curses = self.curses
        value = 0
        for word in style.split():
            if word == "bold":
                value |= curses.A_BOLD
            elif word in ("dim", "contact"):  # design 26.6: a contact's row is dim
                value |= curses.A_DIM
            elif word == "reverse":
                value |= curses.A_REVERSE
            elif word in PAIRS and self.colours:
                value |= curses.color_pair(PAIRS[word])
        return value

    def draw(self, lines: Sequence[Line]) -> None:
        cols, rows = self.size()
        screen = self.stdscr
        screen.erase()
        for y, line in enumerate(list(lines)[:rows]):
            x = 0
            for text, style in line:
                if x >= cols or not text:
                    continue
                if width_of(text) > cols - x:
                    text = cut(text, cols - x)
                try:  # the bottom-right cell cannot take a character without an error
                    screen.addstr(y, x, text, self.attr(style))
                except self.curses.error:
                    pass
                x += width_of(text)
        screen.refresh()

    def getch(self, timeout_ms: int):
        curses = self.curses
        self.stdscr.timeout(max(0, int(timeout_ms)))
        code = self.stdscr.getch()
        if code == -1:
            return None
        if code == getattr(curses, "KEY_MOUSE", None):
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return None
            pressed = getattr(curses, "BUTTON1_PRESSED", 0) | getattr(curses, "BUTTON1_CLICKED", 0)
            return (CLICK, y, x) if state & pressed else None
        if code == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
            except (AttributeError, curses.error):
                pass
            self.stdscr.clear()
            return RESIZE
        named = {curses.KEY_UP: UP, curses.KEY_DOWN: DOWN, getattr(curses, "KEY_LEFT", -2): LEFT,
                 getattr(curses, "KEY_RIGHT", -3): RIGHT, getattr(curses, "KEY_PPAGE", -4): PAGEUP,
                 getattr(curses, "KEY_NPAGE", -5): PAGEDOWN, getattr(curses, "KEY_BACKSPACE", -6): BACKSPACE,
                 127: BACKSPACE, 8: BACKSPACE}
        if code == 27:
            return self.escape()
        if code in named:
            return named[code]
        if code in (curses.KEY_ENTER, 10, 13):
            return ENTER
        if 32 <= code < 127:
            return chr(code)
        return "code:%d" % code

    def follow(self) -> int:
        """The next byte of a sequence already under way, or -1 when none comes soon."""
        self.stdscr.timeout(ESC_FOLLOW_MS)
        return self.stdscr.getch()

    def escape(self):
        """After an Esc that curses did not turn into a key (design 25.8): a lone Esc; a
        click in SGR (``ESC [ < b ; x ; y M``) or X10 (``ESC [ M b x y``) encoding, press of
        the left button only, as ``("click", row, col)``; anything else (a release, another
        button, a sequence curses does not know, Alt with a key) is dropped whole, so none of
        its bytes, digits above all, reaches the board as a key."""
        first = self.follow()
        if first == -1:
            return ESC
        if first == ord("O"):  # SS3: one more byte
            self.follow()
            return None
        if first != ord("["):
            return None  # Alt with a key, or a stray byte
        second = self.follow()
        if second == ord("<"):
            text = ""
            while len(text) < SEQUENCE_MAX:
                code = self.follow()
                if code == -1 or not 0 <= code < 128:
                    return None
                if chr(code) in "Mm":
                    return sgr_click(text, chr(code))
                text += chr(code)
            self.drain()
            return None
        if second == ord("M"):
            raw = [self.follow() for _ in range(3)]
            if -1 in raw:
                return None
            button, x, y = (value - 32 for value in raw)
            return (CLICK, y - 1, x - 1) if button & BUTTON_BITS == 0 else None
        code, count = second, 0
        while code != -1 and not 0x40 <= code <= 0x7E and count < SEQUENCE_MAX:  # up to a CSI's final byte
            code = self.follow()
            count += 1
        return None

    def drain(self) -> None:
        """Drop what is left of a sequence too long to be one."""
        while self.follow() != -1:
            pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        curses = self.curses
        for step in (lambda: curses.mousemask(0), lambda: self.stdscr.keypad(False), curses.nocbreak, curses.echo,
                     lambda: curses.curs_set(1), curses.endwin):
            try:
                step()
            except (curses.error, AttributeError):
                pass


def zoom(herdr, env: Mapping[str, str], watch_pane: Optional[str]) -> Optional[str]:
    """Zoom the strip, or restore it (design 25.1): ``pane zoom <pane> --toggle`` on the
    strip's own pane, named by ``HERDR_PANE_ID`` (set in the strip's process) or else by the
    state file's watch entry. Never ``--current``: herdr resolves that to the pane the owner's
    UI has focused, not to the calling process (found live, 25.7). Returns the pane toggled,
    or None when none is known and nothing was run. HerdrError goes to the caller."""
    pane = env.get("HERDR_PANE_ID") or watch_pane
    if not pane:
        return None
    herdr.pane_zoom_toggle(pane)
    return pane


def open_screen(env: Mapping[str, str], stream=None) -> Optional[CursesScreen]:
    """Start curses on the terminal behind ``stream`` (stdout), or None when it cannot be
    started; the watcher then prints its lines (design 24.1)."""
    try:
        import curses
    except ImportError:
        return None
    try:
        locale.setlocale(locale.LC_ALL, "")  # ncurses draws UTF-8 only with the locale set
    except locale.Error:
        pass
    if "ESCDELAY" not in os.environ and not hasattr(curses, "set_escdelay"):
        os.environ["ESCDELAY"] = str(ESC_DELAY_MS)  # Python 3.8: read by ncurses when it starts
    try:
        fd = stream.fileno() if stream is not None else 1
        curses.setupterm(env.get("TERM") or os.environ.get("TERM"), fd)  # raises instead of exiting
        stdscr = curses.initscr()
    except (curses.error, AttributeError, OSError, ValueError):
        return None
    screen = CursesScreen(curses, stdscr, False)
    try:
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        try:  # design 25.1: clicks; Esc without its default one-second wait
            curses.mouseinterval(0)
            curses.mousemask(getattr(curses, "BUTTON1_PRESSED", 0) | getattr(curses, "BUTTON1_CLICKED", 0))
        except (AttributeError, curses.error):
            pass
        try:
            curses.set_escdelay(ESC_DELAY_MS)
        except (AttributeError, curses.error):
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if colours_wanted(env) and curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            curses.init_pair(PAIRS["red"], curses.COLOR_RED, background)
            curses.init_pair(PAIRS["green"], curses.COLOR_GREEN, background)
            curses.init_pair(PAIRS["yellow"], curses.COLOR_YELLOW, background)
            screen.colours = True
    except curses.error:
        screen.close()
        return None
    return screen


class FakeScreen:
    """A Screen for tests (design 24.5): records draws, replays scripted keys."""

    def __init__(self, keys: Sequence = (), size: Tuple[int, int] = (162, 6), clock=None,
                 on_key: Optional[Callable[[str], None]] = None, idle: int = 40, colours: bool = True):
        self.script = list(keys)
        self.width, self.height = size
        self.clock = clock
        self.on_key = on_key
        self.idle = idle
        self.colours = colours
        self.draws: List[List[Line]] = []
        self.timeouts: List[int] = []
        self.closed = False

    def size(self) -> Tuple[int, int]:
        return self.width, self.height

    def draw(self, lines: Sequence[Line]) -> None:
        self.draws.append([list(line) for line in lines])

    def _advance(self, seconds: float) -> None:
        if self.clock is not None:
            self.clock.sleep(seconds)

    def getch(self, timeout_ms: int):
        self.timeouts.append(timeout_ms)
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                item()
                continue
            if isinstance(item, tuple) and item and item[0] == CLICK:
                if self.on_key is not None:
                    self.on_key("click:%d:%d" % (int(item[1]), int(item[2])))
                return (CLICK, int(item[1]), int(item[2]))
            if isinstance(item, tuple) and item and item[0] == "resize":
                self.width, self.height = int(item[1]), int(item[2])
                item = RESIZE
            elif isinstance(item, tuple) and item and item[0] == "wait":
                self._advance(float(item[1]))
                return None
            if self.on_key is not None:
                self.on_key(str(item))
            return str(item)
        if self.idle <= 0:
            raise KeyboardInterrupt
        self.idle -= 1
        self._advance(timeout_ms / 1000.0)
        return None

    def close(self) -> None:
        self.closed = True

    def text(self, index: int = -1) -> List[str]:
        """The text of one recorded draw (the last by default), trailing blanks dropped."""
        return ["".join(text for text, _ in line).rstrip() for line in self.draws[index]]
