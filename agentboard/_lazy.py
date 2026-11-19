"""Import a sibling module on first attribute access, so `ab snapshot` (and other quick commands) don't pay for
the harness adapters, GitHub sync and the runner's helpers they never touch."""
import importlib


class Lazy:
    def __init__(self, name, absolute=False):
        self.__dict__["_name"] = name
        self.__dict__["_abs"] = absolute
        self.__dict__["_mod"] = None

    def __getattr__(self, attr):
        mod = self.__dict__["_mod"]
        if mod is None:
            mod = self.__dict__["_mod"] = (importlib.import_module(self.__dict__["_name"]) if self.__dict__["_abs"]
                                          else importlib.import_module("." + self.__dict__["_name"], __package__))
        return getattr(mod, attr)
