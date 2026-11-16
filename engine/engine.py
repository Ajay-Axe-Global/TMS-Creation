"""
MappingFlow: a client's saved mapping, dressed as a client file. helpers/order_builder only ever calls
general(v) / lines(v) / cargo(v) / remarks(v) and reads SAME_FOR_ALL, so an object with those is all it needs — the
builder, the flow and the queue do not know whether a client's text comes from this or from clients/<slug>/<slug>.py.

Each function returns exactly what a client file's function returns (see helpers/order_builder.py); a field with no
recipe is left out, so the flow does not type it.
"""
from engine import catalog
from engine.recipe import DEFAULT_SETTINGS, render


class MappingFlow:
    def __init__(self, rows: dict, settings: dict | None = None, name: str = "mapping"):
        self.__name__ = name
        self.rows = rows
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.SAME_FOR_ALL = list(self.settings["same_for_all"])

    def _text(self, key: str, v) -> str:
        return render(self.rows.get(key), v)

    def general(self, v) -> dict:
        out = {}
        for f in catalog.GENERAL:
            if f.mappable and f"general.{f.key}" in self.rows:
                out[f.key] = self._text(f"general.{f.key}", v)
        return out

    def lines(self, v) -> dict:
        out = {}
        for block, action in (("depot_out", "DEPOT OUT"), ("depot_in", "DEPOT IN")):
            if f"lines.{block}.ref" not in self.rows and f"lines.{block}.date" not in self.rows:
                continue
            date = self._text(f"lines.{block}.date", v)
            line = {"ref": self._text(f"lines.{block}.ref", v), "bookedFrom": date}
            if date and self.settings[f"{block}_until_same"]:
                line["bookedUntil"] = date
            out[action] = line
        if self.settings["others_enabled"] and "lines.others.date" in self.rows:
            date = self._text("lines.others.date", v)
            out["OTHERS"] = {"bookedFrom": date, "bookedUntil": date if self.settings["others_until_same"] else ""}
        return out

    def cargo(self, v) -> dict:
        return {f.key: self._text(f"cargo.{f.key}", v) for f in catalog.CARGO if f"cargo.{f.key}" in self.rows}

    def remarks(self, v) -> str:
        return self._text("remarks.text", v)
