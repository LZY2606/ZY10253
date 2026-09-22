"""领域状态模型。

容量一律使用 ``fractions.Fraction``（有理微升）表达，禁止使用 float
参与任何余额计算，避免浮点累积让容量凭空增减。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Tuple


ZERO = Fraction(0)


def frac(value: Any) -> Fraction:
    """把整数 / 字符串 / Fraction 安全转换为 Fraction（拒绝 float）。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        raise ValueError("容量必须是有理值，禁止 float 累积")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        if "." in text:
            whole, frac_part = text.split(".", 1)
            scale = 10 ** len(frac_part)
            return Fraction(int(whole) * scale + int(frac_part or "0"), scale)
        return Fraction(int(text), 1)
    raise TypeError(f"无法解释为有理容量: {value!r}")


def exact_text(amount: Fraction) -> str:
    """精确的可往返序列化文本（p/q 或整数）。"""
    if amount.denominator == 1:
        return str(amount.numerator)
    return f"{amount.numerator}/{amount.denominator}"


def human_text(amount: Fraction) -> str:
    """面向页面的短展示：有限小数优先，否则显示精确分数。"""
    if amount.denominator == 1:
        return str(amount.numerator)
    decimal = amount.numerator / amount.denominator
    if amount.denominator in (2, 4, 5, 8, 10, 16, 20, 25, 40, 50, 100, 125, 200, 250, 500, 1000):
        return f"{float(decimal):.4f}".rstrip("0").rstrip(".")
    return exact_text(amount)


@dataclass(frozen=True)
class Well:
    slot: str
    name: str
    capacity: Fraction
    contents: Dict[str, Fraction] = field(default_factory=dict)

    @property
    def total(self) -> Fraction:
        return sum(self.contents.values(), ZERO)

    @property
    def free(self) -> Fraction:
        return self.capacity - self.total

    def with_contents(self, contents: Dict[str, Fraction]) -> "Well":
        return replace(self, contents=dict(contents))


@dataclass(frozen=True)
class Container:
    slot: str
    kind: str
    label: str
    wells: Tuple[Well, ...]

    def well(self, name: str) -> Well:
        for well in self.wells:
            if well.name == name:
                return well
        raise KeyError(f"容器 {self.slot} 无孔 {name}")

    def update_well(self, well: Well) -> "Container":
        wells = tuple(well if item.name == well.name else item for item in self.wells)
        return replace(self, wells=wells)


@dataclass(frozen=True)
class Tip:
    rack_slot: str
    position: str
    state: str = "rack"  # rack | picked | empty | waste
    held: Dict[str, Fraction] = field(default_factory=dict)

    @property
    def held_total(self) -> Fraction:
        return sum(self.held.values(), ZERO)

    def as_state(self, state: str, held: Optional[Dict[str, Fraction]] = None) -> "Tip":
        return replace(self, state=state, held=dict(held if held is not None else self.held))


@dataclass(frozen=True)
class LabState:
    clock: Fraction
    containers: Dict[str, Container]
    tips: Dict[str, Tuple[Tip, ...]]
    head_slot: str
    picked_tip: Optional[str]
    tip_capacity: Fraction
    home_slot: str

    def well(self, ref: str) -> Well:
        slot, well_name = ref.split(":", 1)
        return self.containers[slot].well(well_name)

    def set_well(self, well: Well) -> "LabState":
        containers = dict(self.containers)
        containers[well.slot] = containers[well.slot].update_well(well)
        return replace(self, containers=containers)

    def tip(self, tip_id: str) -> Tip:
        rack_slot, position = tip_id.split(":", 1)
        for tip in self.tips[rack_slot]:
            if tip.position == position:
                return tip
        raise KeyError(f"无吸头 {tip_id}")

    def set_tip(self, tip: Tip) -> "LabState":
        racks = dict(self.tips)
        racks[tip.rack_slot] = tuple(
            tip if item.position == tip.position else item for item in racks[tip.rack_slot]
        )
        return replace(self, tips=racks)

    def wells(self) -> Iterable[Tuple[str, Well]]:
        for container in self.containers.values():
            for well in container.wells:
                yield f"{container.slot}:{well.name}", well

    def all_components(self) -> List[str]:
        components = {"liquid"}
        for _, well in self.wells():
            components.update(well.contents)
        for rack in self.tips.values():
            for tip in rack:
                components.update(tip.held)
        return sorted(components)

    def to_json(self) -> Dict[str, Any]:
        return {
            "clock": exact_text(self.clock),
            "head_slot": self.head_slot,
            "picked_tip": self.picked_tip,
            "tip_capacity": exact_text(self.tip_capacity),
            "home_slot": self.home_slot,
            "containers": {
                slot: {
                    "slot": slot,
                    "kind": container.kind,
                    "label": container.label,
                    "wells": [
                        {
                            "name": well.name,
                            "capacity": exact_text(well.capacity),
                            "total": exact_text(well.total),
                            "free": exact_text(well.free),
                            "contents": {
                                component: exact_text(amount)
                                for component, amount in sorted(well.contents.items())
                            },
                        }
                        for well in container.wells
                    ],
                }
                for slot, container in self.containers.items()
            },
            "tips": {
                rack_slot: [
                    {
                        "id": f"{rack_slot}:{tip.position}",
                        "position": tip.position,
                        "state": tip.state,
                        "held": {
                            component: exact_text(amount)
                            for component, amount in sorted(tip.held.items())
                        },
                        "held_total": exact_text(tip.held_total),
                    }
                    for tip in rack
                ]
                for rack_slot, rack in self.tips.items()
            },
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "LabState":
        containers: Dict[str, Container] = {}
        for slot, item in data["containers"].items():
            wells = tuple(
                Well(
                    slot=slot,
                    name=well["name"],
                    capacity=frac(well["capacity"]),
                    contents={
                        component: frac(amount) for component, amount in well["contents"].items()
                    },
                )
                for well in item["wells"]
            )
            containers[slot] = Container(
                slot=slot, kind=item["kind"], label=item["label"], wells=wells
            )
        tips = {
            rack_slot: tuple(
                Tip(
                    rack_slot=rack_slot,
                    position=tip["position"],
                    state=tip["state"],
                    held={component: frac(amount) for component, amount in tip["held"].items()},
                )
                for tip in rack
            )
            for rack_slot, rack in data["tips"].items()
        }
        return cls(
            clock=frac(data["clock"]),
            containers=containers,
            tips=tips,
            head_slot=data["head_slot"],
            picked_tip=data["picked_tip"],
            tip_capacity=frac(data["tip_capacity"]),
            home_slot=data["home_slot"],
        )
