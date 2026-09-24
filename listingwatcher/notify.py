"""ntfy notifications (JSON publish, clean UTF-8 in titles) and message formatting.

One Notifier per watch: its own topic (optional) and an `enabled` toggle. A silent watch is
stored and visible in the interface, but sends neither alerts nor digests."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from .models import Decision, Listing, ModelInfo
from .profiles.base import Profile

log = logging.getLogger("listingwatcher.notify")

_PRIORITY = {"urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}


def eur(v: float | None) -> str:
    if v is None:
        return "?"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return (s[:-3] if s.endswith(",00") else s) + " €"


class Notifier:
    def __init__(self, base_url: str, topic: str, token: str = "", dry_run: bool = False,
                 source_labels: dict[str, str] | None = None, timeout: float = 15.0,
                 profile: Profile | None = None, digest_title: str = "Digest annonces",
                 enabled: bool = True, label: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.topic = topic
        self.token = token
        self.dry_run = dry_run or not (self.base_url and self.topic)
        self.labels = source_labels or {}
        self.timeout = timeout
        self.profile = profile or Profile()
        self.digest_title = digest_title
        self.enabled = enabled
        self.label = label                       # [label] prefix on titles, when several watches notify
        self.sent: list[dict[str, Any]] = []      # useful in tests and in --dry-run

    # ------------------------------------------------------------------ transport
    def send(self, title: str, message: str, priority: str = "default", tags: list[str] | None = None,
             click: Optional[str] = None) -> bool:
        payload: dict[str, Any] = {
            "topic": self.topic, "title": title, "message": message,
            "priority": _PRIORITY.get(priority, 3), "tags": tags or [],
        }
        if click:
            payload["click"] = click
            payload["actions"] = [{"action": "view", "label": "Ouvrir l'annonce", "url": click}]
        self.sent.append(payload)
        if self.dry_run:
            log.info("[dry-run] ntfy %s | %s\n%s", priority, title, message)
            return True
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(3):
            try:
                r = httpx.post(self.base_url, json=payload, headers=headers, timeout=self.timeout)
                if r.status_code < 300:
                    return True
                log.warning("ntfy HTTP %s: %s", r.status_code, r.text[:200])
            except httpx.HTTPError as e:
                log.warning("ntfy network error: %s", e)
            time.sleep(2 * (attempt + 1))
        return False

    # ------------------------------------------------------------------ messages
    def listing_alert(self, listing: Listing, info: ModelInfo, decision: Decision, event: str,
                      prev_unit: float | None = None) -> bool:
        if not self.enabled:
            return False
        src = self.labels.get(listing.source, listing.source.upper())
        suspicious = bool(decision.suspicious)
        prefix = ""
        tags = [listing.source] + list(decision.tags)
        priority = decision.priority
        if event == "price_drop":
            prefix = "↓ "
            tags.append("arrow_down")
        if suspicious:
            prefix = "⚠️ " + prefix
            tags.append("warning")
            priority = "low"
        if self.label:
            prefix += f"[{self.label}] "
        qty = f" ×{info.quantity}" if info.quantity > 1 else ""
        per = self.profile.format_per_unit(decision.per_unit)
        title = f"{prefix}[{src}] {info.label} — {eur(decision.unit_price)}{qty}" + (f" ({per})" if per else "")

        lines = [f"Modèle : {info.label}"]
        ship = "inconnu" if listing.shipping is None else eur(listing.shipping) + (" est." if listing.shipping_estimated else "")
        fees = f" + frais {eur(listing.fees)}" if listing.fees else ""
        lines.append(f"Prix rendu : {eur(listing.delivered_price)} ({eur(listing.price)} + port {ship}{fees})")
        if info.quantity > 1:
            lines.append(f"Lot de {info.quantity} → {eur(decision.unit_price)} par article")
        if prev_unit is not None and event == "price_drop":
            lines.append(f"Avant : {eur(prev_unit)} par article")
        if listing.condition:
            lines.append(f"État : {listing.condition}")
        seller = listing.seller_name or "?"
        if listing.seller_rating is not None or listing.seller_reviews is not None:
            rating = f"{listing.seller_rating:.0f} %" if listing.seller_rating is not None else "?"
            seller += f" ({rating}, {listing.seller_reviews if listing.seller_reviews is not None else '?'} avis)"
        if listing.seller_type:
            seller += f" · {listing.seller_type}"
        lines.append(f"Vendeur : {seller}")
        if listing.location:
            lines.append(f"Lieu : {listing.location}")
        if listing.delivery is not None:
            lines.append("Livraison : " + ("oui" if listing.delivery else "non"))
        if listing.quantity and listing.quantity > 1:
            lines.append(f"Stock : {listing.quantity}")
        if listing.returns:
            lines.append(f"Retours : {listing.returns}")
        lines.extend(self.profile.attr_lines(info))
        for n in decision.notes:
            lines.append(f"• {n}")
        for s in decision.suspicious:
            lines.append(f"⚠ {s}")
        return self.send(title, "\n".join(lines), priority, tags, click=listing.url)

    def digest(self, rows: list[dict[str, Any]], send_if_empty: bool = False) -> bool:
        if not self.enabled:
            return False
        if not rows and not send_if_empty:
            return False
        if not rows:
            return self.send(self.digest_title, "Aucune annonce active retenue.", "low", ["clipboard"])
        lines = []
        for r in rows[:25]:
            src = self.labels.get(r["source"], r["source"].upper())
            label = " ".join(x for x in (r.get("family"), r.get("model")) if x) or "Modèle inconnu"
            qty = f" ×{r['quantity']}" if (r.get("quantity") or 1) > 1 else ""
            lines.append(f"{eur(r['unit_price'])}{qty} · [{src}] {label} · {r.get('location') or ''}\n{r['url']}")
        more = f"\n… et {len(rows) - 25} autres" if len(rows) > 25 else ""
        title = f"{self.digest_title} — {len(rows)} annonce{'s' if len(rows) > 1 else ''} active{'s' if len(rows) > 1 else ''}"
        return self.send(title, "\n".join(lines) + more, "low", ["clipboard"])

    def degraded(self, source: str, error: str, hours: float) -> bool:
        src = self.labels.get(source, source)
        msg = (f"{src} a répondu par un blocage ({error}). Source mise en pause {hours:.0f} h.\n"
               "Mode dégradé : activer « Sauvegarder la recherche » sur leboncoin pour recevoir leurs alertes mail."
               if source == "lbc" else f"{src} indisponible ({error}). Source mise en pause {hours:.0f} h.")
        return self.send(f"Veille annonces : {src} bloqué", msg, "default", ["no_entry"])
