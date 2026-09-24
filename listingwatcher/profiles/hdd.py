"""Profil disques durs : catalogue de références, règles SMR/SAS/4Kn, capacité cible, prix au To."""
from __future__ import annotations

from typing import Any

from ..models import ModelInfo
from ..normalize import Classifier
from .base import Profile


class HddProfile(Profile):
    name = "hdd"
    unit_label = "€/To"
    attr_labels = {"smart_hours": "SMART (h)", "capacities_tb": "Capacités citées (To)"}

    def __init__(self, pcfg: dict[str, Any] | None = None):
        super().__init__(pcfg)
        self.target_tb = float(self.cfg.get("target_capacity_tb", 8))
        self.unit_divisor = self.target_tb
        self.classifier = Classifier(self.cfg.get("models"), self.cfg.get("keywords"), self.target_tb)

    def classify(self, title: str, description: str = "", condition_code: str = "") -> ModelInfo:
        return self.classifier.classify(title, description, condition_code)
