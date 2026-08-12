"""Conversation support for Mammouth AI."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Literal

from homeassistant.components.conversation import (
    ChatLog,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers import intent, template

from .const import CONF_BASE_CONTEXT, CONF_LLM_HASS_API, CONF_PROMPT, DEFAULT_PROMPT, DOMAIN
from .coordinator import MammouthDataUpdateCoordinator
from .ha_tools import HA_TOOLS_SCHEMA, async_dispatch_tool
from .memory import MammouthMemory

_LOGGER = logging.getLogger(__name__)

# Nombre maximum d'aller-retours modèle <-> outils avant d'abandonner, pour
# éviter une boucle infinie si le modèle s'entête à appeler des outils.
MAX_TOOL_ITERATIONS = 8

# Nombre de tours (paires user/assistant) conservés en mémoire par conversation.
# On ne garde que le texte final de chaque tour (pas les appels d'outils
# intermédiaires) : ça reste léger et évite tout risque de laisser un
# tool_call sans réponse associée quand on tronque l'historique.
MAX_HISTORY_TURNS = 10

TOOLS_SYSTEM_SUFFIX = (
    "\n\n---\n"
    "Tu es aussi un assistant de configuration et de contrôle pour cette instance Home "
    "Assistant. Tu as accès à des outils pour :\n"
    "- explorer les zones, appareils et entités et leur état actuel ;\n"
    "- consulter les automatisations, scripts et dashboards déjà existants ;\n"
    "- créer, modifier ou supprimer des automatisations et des scripts (effet immédiat, sans redémarrage) ;\n"
    "- générer des dashboards ;\n"
    "- agir immédiatement sur un appareil (allumer/éteindre/basculer/régler) via call_service ;\n"
    "- mémoriser durablement une information avec remember_fact, pour t'en souvenir dans les "
    "conversations futures (pas seulement celle-ci).\n"
    "Règles à suivre :\n"
    "1. Base toujours tes réponses sur l'état réel de l'installation : utilise les outils de lecture "
    "avant de proposer, créer ou contrôler quoi que ce soit, ne suppose jamais l'existence d'une entité.\n"
    "2. Avant d'agir sur une entité ou de créer une automatisation/script, vérifie que l'entity_id existe "
    "réellement (via get_ha_overview, search_entities ou get_entity_details). Si une recherche dans un "
    "domaine ne donne rien, élargis avec search_entities SANS filtre de domaine avant de conclure que "
    "l'entité n'existe pas : elle peut être dans un domaine différent de celui attendu (ex: switch au "
    "lieu de light).\n"
    "3. Quand l'utilisateur donne un ordre direct et sans ambiguïté (« éteins X », « allume Y », "
    "« monte le volet Z »), exécute l'action tout de suite avec call_service : ne redemande PAS "
    "confirmation avant d'agir. Ne demande une clarification que si plusieurs entités correspondent "
    "de façon ambiguë, ou si l'action est destructrice (ex: suppression d'une automatisation).\n"
    "4. Quand on te demande des suggestions d'amélioration ou d'automatisation, explore d'abord "
    "l'instance pour proposer des idées pertinentes et concrètes plutôt que génériques.\n"
    "5. Après toute création, modification ou action, résume clairement ce qui a été fait (nom, "
    "entity_id, effet) en langage simple.\n"
    "6. Si un outil retourne une erreur, explique-la à l'utilisateur et corrige ta requête si possible.\n"
    "7. Tu as accès à l'historique de cette conversation : ne redemande pas une information que "
    "l'utilisateur ou toi-même avez déjà donnée plus haut dans l'échange.\n"
    "8. Utilise remember_fact pour retenir durablement : une correction de l'utilisateur sur ta "
    "configuration (ex: 'le plafonnier du bureau est un switch, pas une light'), une préférence "
    "exprimée, ou un fait stable sur l'installation. N'utilise PAS remember_fact pour un état "
    "temporaire (une lumière allumée/éteinte change tout le temps, ce n'est pas un souvenir utile) "
    "ni pour une information déjà présente dans le contexte de base ci-dessus."
)

MEMORY_TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "Enregistre durablement une information apprise pendant cette conversation, pour "
                "t'en souvenir dans TOUTES les conversations futures, pas seulement celle-ci. "
                "Utilise ceci pour les corrections de l'utilisateur, ses préférences, ou des faits "
                "stables sur son installation. Ne pas utiliser pour un état temporaire (une lumière "
                "allumée/éteinte change tout le temps, ce n'est pas un souvenir utile)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Le fait à retenir, en une phrase claire et autonome.",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_remembered_facts",
            "description": "Liste tout ce qui a été retenu durablement des conversations précédentes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_fact",
            "description": "Supprime un souvenir précis par son id (voir list_remembered_facts).",
            "parameters": {
                "type": "object",
                "properties": {"fact_id": {"type": "string"}},
                "required": ["fact_id"],
            },
        },
    },
]


class MammouthConversationEntity(ConversationEntity):
    """Mammouth AI conversation entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: MammouthDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__()
        self.coordinator = coordinator
        self._config_entry = config_entry
        self._attr_name = f"Mammouth AI ({config_entry.title})"
        self._attr_unique_id = config_entry.entry_id

        # Historique conversationnel léger, gardé en mémoire par conversation_id.
        # Chaque entrée est une paire {"role": "user"/"assistant", "content": str}.
        # On ne persiste pas les tool_calls intermédiaires : uniquement le
        # message utilisateur et la réponse finale de chaque tour.
        self._histories: dict[str, list[dict]] = {}

        # Mémoire persistante inter-conversations (souvenirs auto-appris),
        # stockée sur disque via le Store natif de Home Assistant.
        self._memory = MammouthMemory(hass, config_entry.entry_id)

        if config_entry.options.get(CONF_LLM_HASS_API, False):
            self._attr_supported_features = ConversationEntityFeature.CONTROL

    @property
    def attribution(self) -> str:
        """Return the attribution."""
        return "Powered by Mammouth AI"

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log: ChatLog
    ) -> ConversationResult:
        """Handle a conversation message."""
        intent_response = intent.IntentResponse(language=user_input.language)

        # Home Assistant fournit un conversation_id pour enchaîner les tours
        # d'une même conversation ; on en génère un si c'est le premier message.
        conversation_id = user_input.conversation_id or uuid.uuid4().hex

        # Obtenir le prompt système
        system_prompt = self._config_entry.options.get(
            CONF_PROMPT, DEFAULT_PROMPT
        )

        tools_enabled = self._config_entry.options.get(CONF_LLM_HASS_API, False)

        # Si l'option d'accès à Home Assistant est activée, traiter les templates
        # et enrichir le prompt avec les instructions liées aux outils.
        if tools_enabled:
            try:
                # Obtenir les informations utilisateur
                user_name = "Utilisateur"
                if user_input.context and user_input.context.user_id:
                    user = await self.hass.auth.async_get_user(
                        user_input.context.user_id
                    )
                    if user and user.name:
                        user_name = user.name

                # Rendre le template avec les variables HA
                system_prompt = template.Template(
                    system_prompt, self.hass
                ).async_render(
                    {
                        "ha_name": self.hass.config.location_name,
                        "user_name": user_name,
                    },
                    parse_result=False,
                )
            except TemplateError as err:
                _LOGGER.error("Error rendering prompt template: %s", err)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    f"Erreur de template: {err}",
                )
                return ConversationResult(
                    response=intent_response,
                    conversation_id=conversation_id,
                )

            system_prompt += TOOLS_SYSTEM_SUFFIX

            await self._memory.async_load()
            system_prompt += self._memory.facts_as_prompt_block()

        # Le contexte de base (défini par l'utilisateur, façon "Projet" ou
        # "Mammouth personnalisé") s'applique toujours, indépendamment de
        # l'accès aux outils Home Assistant.
        base_context = (self._config_entry.options.get(CONF_BASE_CONTEXT) or "").strip()
        if base_context:
            system_prompt += (
                "\n\n---\nContexte de base défini par l'utilisateur pour cet assistant "
                "(à respecter comme un cadre stable) :\n" + base_context
            )

        # Historique propre (sans tool_calls) de cette conversation
        history = self._histories.get(conversation_id, [])

        # Construire les messages pour l'API : system + historique + message courant
        messages: list[dict] = (
            [{"role": "system", "content": system_prompt}]
            + list(history)
            + [{"role": "user", "content": user_input.text}]
        )

        tools = (HA_TOOLS_SCHEMA + MEMORY_TOOLS_SCHEMA) if tools_enabled else None

        _LOGGER.debug("Sending request to Mammouth AI: %s", user_input.text)

        try:
            response_text = await self._async_run_conversation(messages, tools)
            _LOGGER.debug("Received response from Mammouth AI: %s", response_text)
            intent_response.async_set_speech(response_text)

            # Mettre à jour l'historique propre (uniquement les tours finaux,
            # jamais les tool_calls intermédiaires) et le tronquer.
            history = history + [
                {"role": "user", "content": user_input.text},
                {"role": "assistant", "content": response_text},
            ]
            self._histories[conversation_id] = history[-(MAX_HISTORY_TURNS * 2):]

        except HomeAssistantError as err:
            _LOGGER.error("Error processing conversation: %s", err)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Erreur de l'assistant Mammouth: {err}",
            )

        return ConversationResult(
            response=intent_response,
            conversation_id=conversation_id,
        )

    async def _async_run_conversation(
        self, messages: list[dict], tools: list[dict] | None
    ) -> str:
        """Run the model, executing any tool calls it requests, until a final answer."""
        for _ in range(MAX_TOOL_ITERATIONS):
            message = await self.coordinator.async_chat_completion(
                messages, tools=tools
            )
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                return message.get("content") or ""

            # On rajoute le message assistant (avec ses tool_calls) à l'historique
            # de travail de ce tour (pas à l'historique persistant entre tours).
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                function = call.get("function", {})
                fn_name = function.get("name")
                raw_args = function.get("arguments") or "{}"
                try:
                    fn_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    fn_args = {}
                    _LOGGER.warning(
                        "Arguments JSON invalides pour l'outil %s: %s", fn_name, raw_args
                    )

                _LOGGER.debug("Executing tool %s with args %s", fn_name, fn_args)
                result = await self._async_dispatch_tool(fn_name, fn_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        _LOGGER.warning("Maximum tool iterations reached without a final answer")
        return (
            "Désolé, je n'ai pas réussi à conclure cette demande après plusieurs "
            "étapes d'analyse. Peux-tu reformuler ou préciser ta demande ?"
        )

    async def _async_dispatch_tool(self, fn_name: str, fn_args: dict) -> object:
        """Route un appel d'outil vers ha_tools (état HA) ou memory (souvenirs)."""
        if fn_name == "remember_fact":
            return await self._memory.async_add_fact(fn_args.get("text", ""))
        if fn_name == "list_remembered_facts":
            facts = await self._memory.async_list_facts()
            return {"facts": facts}
        if fn_name == "forget_fact":
            return await self._memory.async_remove_fact(fn_args.get("fact_id", ""))
        return await async_dispatch_tool(self.hass, fn_name, fn_args)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Mammouth AI conversation platform."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    entity = MammouthConversationEntity(hass, coordinator, config_entry)
    async_add_entities([entity])
    _LOGGER.debug("Mammouth AI conversation entity added")
