"""Monthly API spend cap.

Har Anthropic call ka token usage record hota hai aur state/budget.json me
month-wise jamaa hota hai. Budget khatam hone par pipeline aage nahi badhti.

NOTE: Ye ek SOFT cap hai — sirf isi script ke kharche ginta hai. Asli hard
limit console.anthropic.com -> Settings -> Limits me set kijiye. Dono lagayein.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CONFIG, ROOT


class BudgetExceeded(Exception):
    """Mahine ka cap poora ho gaya."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class Budget:
    def __init__(self) -> None:
        b = CONFIG["budget"]
        self.enabled: bool = b.get("enabled", True)
        self.cap_inr: float = float(b["monthly_inr"])
        self.usd_inr: float = float(b["usd_inr"])
        self.warn_at: float = float(b.get("warn_at_percent", 80)) / 100
        self.prices: dict = b["prices"]
        self.reserve_inr: float = float(b.get("reserve_per_video_inr", 25))

        self.path = ROOT / b.get("file", "state/budget.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"months": {}}
        self.data.setdefault("months", {})

    # ------------------------------------------------------------------ core

    @property
    def month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    @property
    def month(self) -> dict:
        return self.data["months"].setdefault(
            self.month_key,
            {"input_tokens": 0, "output_tokens": 0, "inr": 0.0, "calls": 0},
        )

    @property
    def spent_inr(self) -> float:
        return float(self.month["inr"])

    @property
    def remaining_inr(self) -> float:
        return max(0.0, self.cap_inr - self.spent_inr)

    def cost_inr(self, model: str, usage: Usage) -> float:
        p = self.prices.get(model)
        if not p:
            # Anjaan model — sabse mehnga rate maano, taaki cap tootay nahi
            p = max(self.prices.values(), key=lambda x: x["output_per_mtok_usd"])
        usd = (
            usage.input_tokens / 1_000_000 * p["input_per_mtok_usd"]
            + usage.output_tokens / 1_000_000 * p["output_per_mtok_usd"]
        )
        return usd * self.usd_inr

    def record(self, model: str, usage: Usage) -> float:
        inr = self.cost_inr(model, usage)
        m = self.month
        m["input_tokens"] += usage.input_tokens
        m["output_tokens"] += usage.output_tokens
        m["inr"] = round(float(m["inr"]) + inr, 4)
        m["calls"] += 1
        self.save()
        return inr

    # ----------------------------------------------------------------- gates

    def check_before_video(self) -> None:
        """Naya video shuru karne se pehle. Aadha video banakar rukna
        bekaar hai, isliye ek poore video ka kharcha reserve rakhte hain."""
        if not self.enabled:
            return
        if self.remaining_inr < self.reserve_inr:
            raise BudgetExceeded(
                f"{self.month_key} ka budget khatam: "
                f"Rs {self.spent_inr:.2f} / Rs {self.cap_inr:.2f} kharch ho chuke. "
                f"Ek aur video ke liye kam se kam Rs {self.reserve_inr:.0f} chahiye. "
                f"Agle mahine apne aap reset ho jayega. "
                f"Abhi badhana ho to config.yaml me budget.monthly_inr badlein."
            )

    def check_hard_stop(self) -> None:
        """Har call se pehle — cap paar ho chuka ho to turant ruk jao."""
        if self.enabled and self.spent_inr >= self.cap_inr:
            raise BudgetExceeded(
                f"{self.month_key}: cap Rs {self.cap_inr:.2f} paar ho gaya "
                f"(Rs {self.spent_inr:.2f} kharch). Ruk raha hoon."
            )

    def status_line(self) -> str:
        pct = (self.spent_inr / self.cap_inr * 100) if self.cap_inr else 0
        warn = "  <-- WARNING" if pct >= self.warn_at * 100 else ""
        return (
            f"[budget] {self.month_key}: Rs {self.spent_inr:.2f} / "
            f"Rs {self.cap_inr:.2f} ({pct:.0f}%), "
            f"{self.month['calls']} calls, baaki Rs {self.remaining_inr:.2f}{warn}"
        )

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


_budget: Budget | None = None


def get_budget() -> Budget:
    global _budget
    if _budget is None:
        _budget = Budget()
    return _budget
