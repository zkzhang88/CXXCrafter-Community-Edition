from .init import ensure_all_directories_exist

ensure_all_directories_exist()


def __getattr__(name):
    if name == "CXXCrafter":
        from .cli import CXXCrafter

        return CXXCrafter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CXXCrafter"]
