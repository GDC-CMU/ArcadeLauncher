"""The on-screen startup failure screen.

If the manifest or the settings file is broken, the cabinet must not just sit
on a black screen: nobody is standing at it with a terminal open.  This module
paints a readable, branded error and waits for any button before returning
control, so a club member can photograph it and fix the file.

It is deliberately self-contained -- it must work when the rest of the UI stack
is exactly what failed.
"""

from __future__ import annotations

import logging

from ..controls import BUTTON_EXIT, BUTTON_LAUNCH
from . import SCREEN_SIZE
from .pygame_runtime import pygame
from .theme import FontBook, PALETTE, PixelFont, shade

__all__ = ["show_fatal_screen"]

_log = logging.getLogger(__name__)

#: How long the message stays up if nobody presses anything (milliseconds).
AUTO_DISMISS_MS = 20_000


def show_fatal_screen(
    headline: str, detail: str, *, fullscreen: bool = False, timeout_ms: int = AUTO_DISMISS_MS
) -> None:
    """Display *headline* and *detail* until dismissed or *timeout_ms* elapses."""
    pygame.display.init()
    pygame.font.init()
    try:
        flags = pygame.SCALED | (pygame.FULLSCREEN if fullscreen else 0)
        screen = pygame.display.set_mode(SCREEN_SIZE, flags)
        pygame.display.set_caption("GDC Arcade -- startup failed")
        pygame.mouse.set_visible(False)
        _paint(screen, headline, detail)
        pygame.display.flip()
        _wait(timeout_ms)
    finally:
        pygame.display.quit()
        pygame.font.quit()
        pygame.quit()


def _paint(screen: pygame.Surface, headline: str, detail: str) -> None:
    pixel = PixelFont()
    fonts = FontBook()
    width, height = screen.get_size()

    screen.fill(shade(PALETTE["deep_violet"], 0.55))
    pygame.draw.rect(screen, PALETTE["cmu_red"], pygame.Rect(0, 0, width, 8))
    pygame.draw.rect(screen, PALETTE["cmu_red"], pygame.Rect(0, height - 8, width, 8))

    panel = pygame.Rect(50, 120, width - 100, height - 250)
    pygame.draw.rect(screen, shade(PALETTE["ember_red"], 0.18), panel, border_radius=10)
    pygame.draw.rect(screen, PALETTE["ember_red"], panel, width=2, border_radius=10)

    pixel.draw(
        screen, "ARCADE LAUNCHER", (width // 2, 56), 3, PALETTE["bone"], anchor="midtop"
    )
    pixel.draw(
        screen,
        "COULD NOT START",
        (width // 2, 84),
        2,
        PALETTE["warm_amber"],
        anchor="midtop",
    )

    top = panel.top + 30
    for line in fonts.wrap(headline, "lead", panel.width - 60, max_lines=2):
        text = fonts.render(line, "lead", PALETTE["bone"])
        screen.blit(text, text.get_rect(midtop=(panel.centerx, top)))
        top += text.get_height() + 6

    top += 14
    for line in fonts.wrap(detail, "body", panel.width - 60, max_lines=6):
        text = fonts.render(line, "body", PALETTE["steel"])
        screen.blit(text, (panel.left + 30, top))
        top += text.get_height() + 4

    pixel.draw(
        screen,
        "FIX THE FILE AND RESTART   -   PRESS ANY BUTTON",
        (width // 2, height - 52),
        2,
        PALETTE["electric_cyan"],
        anchor="midbottom",
    )


def _wait(timeout_ms: int) -> None:
    """Block until any button, key or quit event arrives, or we time out."""
    pygame.joystick.init()
    sticks = []
    for index in range(pygame.joystick.get_count()):
        try:
            stick = pygame.joystick.Joystick(index)
            stick.init()
            sticks.append(stick)
        except pygame.error as exc:
            _log.debug("joystick %s unavailable on the error screen: %s", index, exc)

    clock = pygame.time.Clock()
    waited = 0
    try:
        while waited < timeout_ms:
            waited += clock.tick(30)
            for event in pygame.event.get():
                if event.type in (pygame.QUIT, pygame.KEYDOWN):
                    return
                if event.type == pygame.JOYBUTTONDOWN and event.button in (
                    BUTTON_LAUNCH,
                    BUTTON_EXIT,
                ):
                    return
    finally:
        for stick in sticks:
            try:
                stick.quit()
            except pygame.error as exc:
                _log.debug("could not close joystick: %s", exc)
        pygame.joystick.quit()
