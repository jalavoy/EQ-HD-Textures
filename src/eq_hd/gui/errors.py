"""Errors shared by the window's background jobs (no Qt here)."""


class Cancelled(Exception):
    """A long job (download, move) was stopped by the player."""


class UserError(Exception):
    """An error whose message is written for the player: shown as it is."""
