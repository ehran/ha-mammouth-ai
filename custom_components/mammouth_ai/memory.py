"""Mémoire persistante inter-conversations pour Mammouth AI.

Deux notions distinctes, volontairement séparées :

- Le "contexte de base" (voir const.CONF_BASE_CONTEXT) est écrit par
  l'utilisateur dans les options de l'intégration, comme les instructions
  personnalisées d'un Projet Claude ou d'un Mammouth personnalisé. Stable,
  contrôlé par l'humain.
- Les "souvenirs" (facts) sont appris automatiquement par le modèle via
  l'outil remember_fact pendant les conversations, et persistés sur disque
  via le helper Store natif de Home Assistant. Ils s'accumulent avec le
  temps, sans intervention humaine.

On utilise Store plutôt qu'un fichier YAML/JSON fait main : c'est l'API
standard et stable de HA pour ce genre de petites données (écriture atomique,
gestion de version intégrée), déjà utilisée par des centaines d'intégrations
core et custom.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_FACTS = 200


class MammouthMemory:
    """Charge/sauvegarde les souvenirs auto-appris d'une entrée de configuration."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_memory"
        )
        self._facts: list[dict[str, str]] = []
        self._loaded = False

    async def async_load(self) -> None:
        """Charge les souvenirs depuis le disque (une seule fois, puis mise en cache)."""
        if self._loaded:
            return
        data = await self._store.async_load()
        self._facts = (data or {}).get("facts", [])
        self._loaded = True

    async def async_list_facts(self) -> list[dict[str, str]]:
        await self.async_load()
        return list(self._facts)

    async def async_add_fact(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            return {"error": "Le contenu du souvenir ne peut pas être vide."}

        await self.async_load()
        fact = {"id": uuid.uuid4().hex[:8], "text": text.strip()}
        self._facts.append(fact)

        # FIFO : on garde les souvenirs les plus récents pour éviter une
        # croissance illimitée du prompt système au fil du temps.
        if len(self._facts) > MAX_FACTS:
            self._facts = self._facts[-MAX_FACTS:]

        await self._store.async_save({"facts": self._facts})
        return {
            "success": True,
            "id": fact["id"],
            "message": "Souvenir enregistré durablement, il sera rappelé dans les prochaines conversations.",
        }

    async def async_remove_fact(self, fact_id: str) -> dict[str, Any]:
        await self.async_load()
        before = len(self._facts)
        self._facts = [f for f in self._facts if f["id"] != fact_id]

        if len(self._facts) == before:
            return {"error": f"Souvenir {fact_id} introuvable."}

        await self._store.async_save({"facts": self._facts})
        return {"success": True, "message": f"Souvenir {fact_id} oublié."}

    def facts_as_prompt_block(self) -> str:
        """Rendu texte des souvenirs, à injecter dans le prompt système.

        Doit être appelé après async_load()/async_list_facts() pour refléter
        le contenu actuel.
        """
        if not self._facts:
            return ""
        lines = "\n".join(f"- {fact['text']}" for fact in self._facts)
        return (
            "\n\n---\nSouvenirs retenus des conversations précédentes (à réutiliser, "
            "mais à revérifier avec les outils de lecture en cas de doute) :\n" + lines
        )
