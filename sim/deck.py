"""Deck 布局与孔板矩阵 (NumPy)。"""
from __future__ import annotations

import re
from fractions import Fraction

import numpy as np

from .state import fmt_frac

_WELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def deck_occupancy(state: dict):
    """槽位占用矩阵: grid[i][j] =  labware 名或 None。"""
    layout = np.array(state["deck"]["layout"], dtype=int)
    occ = np.empty(layout.shape, dtype=object)
    slots = state["deck"]["slots"]
    for idx in np.ndindex(layout.shape):
        slot_no = str(int(layout[idx]))
        occ[idx] = slots.get(slot_no)
    return {"layout": layout.tolist(), "occupancy": occ.tolist()}


def plate_volume_matrix(state: dict, labware: str, layer: str = "physical"):
    """孔板体积矩阵 (有理微升字符串), 行=A.., 列=1..。"""
    wells = state["wells"].get(labware)
    if not wells:
        return None
    rows, cols = [], []
    for key in wells:
        m = _WELL_RE.match(key)
        rows.append(m.group(1))
        cols.append(int(m.group(2)))
    rows = sorted(set(rows))
    cols = sorted(set(cols))
    matrix = np.empty((len(rows), len(cols)), dtype=object)
    for key, data in wells.items():
        m = _WELL_RE.match(key)
        r, c = rows.index(m.group(1)), cols.index(int(m.group(2)))
        total = sum(data[layer].values(), Fraction(0))
        matrix[r, c] = fmt_frac(total)
    return {"rows": rows, "cols": cols, "matrix": matrix.tolist()}
