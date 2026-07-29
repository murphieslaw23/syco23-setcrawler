from enum import StrEnum


class UserRole(StrEnum):
    viewer = "viewer"
    editor = "editor"
    admin = "admin"
