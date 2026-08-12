"""Outils (function calling) permettant à l'IA de lire et configurer Home Assistant.

Conçu pour rester robuste d'une version de Home Assistant à l'autre : on
s'appuie uniquement sur des API publiques et stables (registres area/device/
entity, services standards, lecture/écriture des fichiers automations.yaml et
scripts.yaml + rechargement via les services natifs "reload"). On évite
volontairement les API internes de validation de schéma ou la collection de
stockage Lovelace, qui changent plus souvent d'une version à l'autre.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from homeassistant.util.yaml import dump as yaml_dump
from homeassistant.util.yaml.loader import load_yaml as yaml_load

_LOGGER = logging.getLogger(__name__)

AUTOMATIONS_FILE = "automations.yaml"
SCRIPTS_FILE = "scripts.yaml"
DASHBOARDS_DIR = "dashboards"

DEFAULT_OVERVIEW_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 50

# ---------------------------------------------------------------------------
# Schéma des outils (format function-calling compatible OpenAI)
# ---------------------------------------------------------------------------

HA_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_ha_overview",
            "description": (
                "Retourne une vue d'ensemble de l'instance Home Assistant : "
                "zones (areas), et les entités qu'elles contiennent avec leur "
                "état actuel. Utilise les filtres domain/area/limit pour "
                "limiter le volume sur une grosse installation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Filtrer par domaine, ex: light, switch, sensor, climate.",
                    },
                    "area": {
                        "type": "string",
                        "description": "Filtrer par nom ou id de zone.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre maximum d'entités retournées (défaut 200).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Recherche des entités par mot-clé dans leur nom ou entity_id. "
                "Utile pour retrouver une entité précise sans charger tout l'inventaire."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Mot-clé recherché."},
                    "domain": {"type": "string", "description": "Filtrer par domaine (optionnel)."},
                    "limit": {"type": "integer", "description": "Nombre max de résultats (défaut 50)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_details",
            "description": "Retourne l'état complet, les attributs, la zone et l'appareil d'une entité précise.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": "Liste toutes les automatisations existantes (id, nom, description, mode).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_automation",
            "description": "Retourne la configuration complète (trigger/condition/action) d'une automatisation existante.",
            "parameters": {
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": (
                "Crée une nouvelle automatisation Home Assistant et la recharge "
                "immédiatement (aucun redémarrage requis). trigger/condition/action "
                "doivent utiliser la syntaxe native Home Assistant (les mêmes clés "
                "que dans un fichier automations.yaml)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "description": "Nom lisible de l'automatisation."},
                    "description": {"type": "string"},
                    "trigger": {"type": "array", "items": {"type": "object"}},
                    "condition": {"type": "array", "items": {"type": "object"}},
                    "action": {"type": "array", "items": {"type": "object"}},
                    "mode": {
                        "type": "string",
                        "enum": ["single", "restart", "queued", "parallel"],
                    },
                },
                "required": ["alias", "trigger", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_automation",
            "description": "Modifie une automatisation existante par id. Seuls les champs fournis sont remplacés.",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string"},
                    "alias": {"type": "string"},
                    "description": {"type": "string"},
                    "trigger": {"type": "array", "items": {"type": "object"}},
                    "condition": {"type": "array", "items": {"type": "object"}},
                    "action": {"type": "array", "items": {"type": "object"}},
                    "mode": {"type": "string"},
                },
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": "Supprime une automatisation existante par id.",
            "parameters": {
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scripts",
            "description": "Liste tous les scripts existants (id, nom, mode).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_script",
            "description": "Crée un nouveau script Home Assistant (séquence d'actions réutilisable) et le recharge immédiatement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {
                        "type": "string",
                        "description": "Identifiant technique (minuscules, underscores). Généré depuis l'alias si absent.",
                    },
                    "alias": {"type": "string"},
                    "sequence": {"type": "array", "items": {"type": "object"}},
                    "mode": {
                        "type": "string",
                        "enum": ["single", "restart", "queued", "parallel"],
                    },
                },
                "required": ["alias", "sequence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dashboards",
            "description": "Liste les dashboards Lovelace générés par cet assistant (mode YAML).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dashboard",
            "description": (
                "Génère un fichier de dashboard Lovelace (mode YAML) dans "
                "config/dashboards/. Si ce dashboard n'est pas encore déclaré "
                "dans configuration.yaml, une étape manuelle unique (ajout + "
                "redémarrage) est nécessaire et sera indiquée dans le résultat ; "
                "les modifications suivantes du même fichier seront prises en "
                "compte automatiquement, sans redémarrage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url_path": {
                        "type": "string",
                        "description": "Slug d'URL, ex: 'energie'. Généré depuis le titre si absent.",
                    },
                    "icon": {"type": "string"},
                    "views": {
                        "type": "array",
                        "description": "Liste de vues Lovelace au format natif (title, path, cards...).",
                        "items": {"type": "object"},
                    },
                },
                "required": ["title", "views"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_service",
            "description": (
                "Exécute une action immédiate sur un ou plusieurs appareils Home Assistant : "
                "allumer/éteindre/basculer une lumière ou un interrupteur, régler une température, "
                "ouvrir/fermer un volet, etc. C'est le seul moyen d'agir réellement sur un appareil "
                "(les autres outils ne font que lire ou configurer). Vérifie l'entity_id exact avec "
                "search_entities ou get_entity_details si tu n'es pas certain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domaine du service, ex: light, switch, climate, cover.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Nom du service, ex: turn_on, turn_off, toggle, set_temperature.",
                    },
                    "entity_id": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Liste des entity_id ciblés (un seul élément si une entité).",
                    },
                    "data": {
                        "type": "object",
                        "description": "Paramètres additionnels du service, ex: {'temperature': 21}.",
                    },
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers de lecture/écriture YAML (bloquants -> à appeler via executor)
# ---------------------------------------------------------------------------


def _read_yaml_list(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml_load(str(p))
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path} ne contient pas une liste YAML valide.")
    return data


def _write_yaml_list(path: str, data: list) -> None:
    Path(path).write_text(yaml_dump(data), encoding="utf-8")


def _read_yaml_dict(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml_load(str(p))
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} ne contient pas un dictionnaire YAML valide.")
    return data


def _write_yaml_dict(path: str, data: dict) -> None:
    Path(path).write_text(yaml_dump(data), encoding="utf-8")


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Lecture de l'instance
# ---------------------------------------------------------------------------


async def _get_ha_overview(
    hass: HomeAssistant,
    domain: str | None = None,
    area: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    limit = limit or DEFAULT_OVERVIEW_LIMIT
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    area_filter_id = None
    if area:
        area_lower = area.lower()
        for a in area_reg.async_list_areas():
            if a.id == area or a.name.lower() == area_lower:
                area_filter_id = a.id
                break
        if area_filter_id is None:
            return {"error": f"Zone '{area}' introuvable."}

    all_states = hass.states.async_all()
    areas_out: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    count = 0
    truncated = False

    for state in all_states:
        if domain and state.domain != domain:
            continue

        entry = ent_reg.async_get(state.entity_id)
        area_id = entry.area_id if entry else None
        if entry and not area_id and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            if device:
                area_id = device.area_id

        if area_filter_id and area_id != area_filter_id:
            continue

        if count >= limit:
            truncated = True
            break

        item = {
            "entity_id": state.entity_id,
            "name": state.name,
            "state": state.state,
            "device_class": state.attributes.get("device_class"),
        }
        count += 1

        if area_id:
            area_entry = area_reg.async_get_area(area_id)
            area_name = area_entry.name if area_entry else area_id
            areas_out.setdefault(area_name, []).append(item)
        else:
            unassigned.append(item)

    return {
        "total_entities_in_instance": len(all_states),
        "entities_returned": count,
        "truncated": truncated,
        "areas": areas_out,
        "entities_without_area": unassigned,
    }


async def _search_entities(
    hass: HomeAssistant,
    query: str,
    domain: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    limit = limit or DEFAULT_SEARCH_LIMIT
    query_lower = query.lower()
    results = []
    for state in hass.states.async_all():
        if domain and state.domain != domain:
            continue
        if query_lower in state.entity_id.lower() or query_lower in (state.name or "").lower():
            results.append(
                {"entity_id": state.entity_id, "name": state.name, "state": state.state}
            )
            if len(results) >= limit:
                break
    return {"results": results, "count": len(results)}


async def _get_entity_details(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    if not state:
        return {"error": f"Entité {entity_id} introuvable."}

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    area_name = None
    device_name = None
    entry = ent_reg.async_get(entity_id)
    if entry:
        area_id = entry.area_id
        if entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            if device:
                device_name = device.name_by_user or device.name
                if not area_id:
                    area_id = device.area_id
        if area_id:
            area_entry = area_reg.async_get_area(area_id)
            area_name = area_entry.name if area_entry else area_id

    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "area": area_name,
        "device": device_name,
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }


# ---------------------------------------------------------------------------
# Automatisations
# ---------------------------------------------------------------------------


async def _list_automations(hass: HomeAssistant) -> list[dict[str, Any]]:
    path = hass.config.path(AUTOMATIONS_FILE)
    items = await hass.async_add_executor_job(_read_yaml_list, path)
    return [
        {
            "id": item.get("id"),
            "alias": item.get("alias"),
            "description": item.get("description", ""),
            "mode": item.get("mode", "single"),
        }
        for item in items
    ]


async def _get_automation(hass: HomeAssistant, automation_id: str) -> dict[str, Any]:
    path = hass.config.path(AUTOMATIONS_FILE)
    items = await hass.async_add_executor_job(_read_yaml_list, path)
    for item in items:
        if str(item.get("id")) == str(automation_id):
            return item
    return {"error": f"Automatisation {automation_id} introuvable."}


async def _find_automation_entity_id(hass: HomeAssistant, automation_id: str, alias: str) -> str | None:
    for state in hass.states.async_all("automation"):
        if str(state.attributes.get("id")) == str(automation_id) or state.name == alias:
            return state.entity_id
    return None


async def _create_automation(
    hass: HomeAssistant,
    alias: str,
    trigger: list,
    action: list,
    description: str = "",
    condition: list | None = None,
    mode: str = "single",
) -> dict[str, Any]:
    if not alias or not trigger or not action:
        return {"error": "alias, trigger et action sont requis et ne peuvent être vides."}

    path = hass.config.path(AUTOMATIONS_FILE)
    items = await hass.async_add_executor_job(_read_yaml_list, path)

    new_id = dt_util.utcnow().strftime("%Y%m%d%H%M%S%f")
    new_item = {
        "id": new_id,
        "alias": alias,
        "description": description or "",
        "trigger": trigger,
        "condition": condition or [],
        "action": action,
        "mode": mode or "single",
    }
    items.append(new_item)

    try:
        await hass.async_add_executor_job(_write_yaml_list, path, items)
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"Écriture de {AUTOMATIONS_FILE} impossible: {err}"}

    await hass.services.async_call("automation", "reload", blocking=True)
    entity_id = await _find_automation_entity_id(hass, new_id, alias)

    return {
        "success": True,
        "id": new_id,
        "entity_id": entity_id,
        "message": f"Automatisation '{alias}' créée et rechargée avec succès.",
    }


async def _update_automation(
    hass: HomeAssistant,
    automation_id: str,
    alias: str | None = None,
    description: str | None = None,
    trigger: list | None = None,
    condition: list | None = None,
    action: list | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    path = hass.config.path(AUTOMATIONS_FILE)
    items = await hass.async_add_executor_job(_read_yaml_list, path)

    for item in items:
        if str(item.get("id")) == str(automation_id):
            updates = {
                "alias": alias,
                "description": description,
                "trigger": trigger,
                "condition": condition,
                "action": action,
                "mode": mode,
            }
            for key, value in updates.items():
                if value is not None:
                    item[key] = value

            await hass.async_add_executor_job(_write_yaml_list, path, items)
            await hass.services.async_call("automation", "reload", blocking=True)
            return {"success": True, "message": f"Automatisation {automation_id} mise à jour."}

    return {"error": f"Automatisation {automation_id} introuvable."}


async def _delete_automation(hass: HomeAssistant, automation_id: str) -> dict[str, Any]:
    path = hass.config.path(AUTOMATIONS_FILE)
    items = await hass.async_add_executor_job(_read_yaml_list, path)
    new_items = [i for i in items if str(i.get("id")) != str(automation_id)]

    if len(new_items) == len(items):
        return {"error": f"Automatisation {automation_id} introuvable."}

    await hass.async_add_executor_job(_write_yaml_list, path, new_items)
    await hass.services.async_call("automation", "reload", blocking=True)
    return {"success": True, "message": f"Automatisation {automation_id} supprimée."}


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


async def _list_scripts(hass: HomeAssistant) -> list[dict[str, Any]]:
    path = hass.config.path(SCRIPTS_FILE)
    data = await hass.async_add_executor_job(_read_yaml_dict, path)
    return [
        {"script_id": key, "alias": value.get("alias", key), "mode": value.get("mode", "single")}
        for key, value in data.items()
    ]


async def _create_script(
    hass: HomeAssistant,
    alias: str,
    sequence: list,
    script_id: str | None = None,
    mode: str = "single",
) -> dict[str, Any]:
    if not alias or not sequence:
        return {"error": "alias et sequence sont requis et ne peuvent être vides."}

    path = hass.config.path(SCRIPTS_FILE)
    data = await hass.async_add_executor_job(_read_yaml_dict, path)

    slug = slugify(script_id or alias)
    original_slug = slug
    i = 2
    while slug in data:
        slug = f"{original_slug}_{i}"
        i += 1

    data[slug] = {"alias": alias, "sequence": sequence, "mode": mode or "single"}

    try:
        await hass.async_add_executor_job(_write_yaml_dict, path, data)
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"Écriture de {SCRIPTS_FILE} impossible: {err}"}

    await hass.services.async_call("script", "reload", blocking=True)
    return {
        "success": True,
        "script_id": slug,
        "entity_id": f"script.{slug}",
        "message": f"Script '{alias}' créé et rechargé avec succès.",
    }


# ---------------------------------------------------------------------------
# Dashboards (mode YAML)
# ---------------------------------------------------------------------------


async def _list_dashboards(hass: HomeAssistant) -> dict[str, Any]:
    dash_dir = Path(hass.config.path(DASHBOARDS_DIR))
    if not dash_dir.exists():
        return {"dashboards": [], "note": "Aucun dashboard généré pour le moment par cet assistant."}
    files = await hass.async_add_executor_job(
        lambda: sorted(p.stem for p in dash_dir.glob("*.yaml"))
    )
    return {"dashboards": files}


async def _create_dashboard(
    hass: HomeAssistant,
    title: str,
    views: list,
    url_path: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    if not title or not views:
        return {"error": "title et views sont requis et ne peuvent être vides."}

    slug = slugify(url_path or title)
    file_path = Path(hass.config.path(DASHBOARDS_DIR)) / f"{slug}.yaml"

    content: dict[str, Any] = {"title": title, "views": views}
    if icon:
        content["icon"] = icon

    try:
        await hass.async_add_executor_job(_write_text_file, file_path, yaml_dump(content))
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"Écriture du dashboard impossible: {err}"}

    config_snippet = (
        "lovelace:\n"
        "  dashboards:\n"
        f"    {slug}:\n"
        "      mode: yaml\n"
        f"      filename: {DASHBOARDS_DIR}/{slug}.yaml\n"
        f"      title: {title}\n"
        + (f"      icon: {icon}\n" if icon else "")
    )

    return {
        "success": True,
        "file": f"{DASHBOARDS_DIR}/{slug}.yaml",
        "message": (
            f"Fichier de dashboard '{slug}.yaml' généré dans config/{DASHBOARDS_DIR}/. "
            "S'il n'est pas déjà déclaré, ajoute une seule fois ce bloc dans configuration.yaml "
            "puis redémarre Home Assistant (les modifications suivantes de ce fichier seront "
            f"ensuite prises en compte automatiquement, sans redémarrage) :\n{config_snippet}"
        ),
    }


# ---------------------------------------------------------------------------
# Contrôle direct (action immédiate)
# ---------------------------------------------------------------------------


async def _call_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_id: str | list[str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not domain or not service or not entity_id:
        return {"error": "domain, service et entity_id sont requis."}

    if isinstance(entity_id, str):
        entity_id = [entity_id]

    missing = [e for e in entity_id if hass.states.get(e) is None]
    if missing:
        return {
            "error": (
                f"Entité(s) introuvable(s): {', '.join(missing)}. "
                "Vérifie l'entity_id exact avec search_entities avant de réessayer."
            )
        }

    service_data = dict(data or {})
    service_data["entity_id"] = entity_id

    try:
        await hass.services.async_call(domain, service, service_data, blocking=True)
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"Échec de l'appel {domain}.{service}: {err}"}

    new_states = {e: hass.states.get(e).state for e in entity_id if hass.states.get(e)}
    return {
        "success": True,
        "message": f"{domain}.{service} exécuté sur {', '.join(entity_id)}.",
        "new_states": new_states,
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_ha_overview": _get_ha_overview,
    "search_entities": _search_entities,
    "get_entity_details": _get_entity_details,
    "list_automations": _list_automations,
    "get_automation": _get_automation,
    "create_automation": _create_automation,
    "update_automation": _update_automation,
    "delete_automation": _delete_automation,
    "list_scripts": _list_scripts,
    "create_script": _create_script,
    "list_dashboards": _list_dashboards,
    "create_dashboard": _create_dashboard,
    "call_service": _call_service,
}


async def async_dispatch_tool(hass: HomeAssistant, name: str, arguments: dict[str, Any]) -> Any:
    """Exécute l'outil demandé par le modèle et retourne un résultat sérialisable en JSON."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"Outil inconnu: {name}"}

    try:
        if handler in (_list_automations, _list_scripts, _list_dashboards):
            return await handler(hass)
        return await handler(hass, **arguments)
    except TypeError as err:
        return {"error": f"Arguments invalides pour {name}: {err}"}
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.exception("Erreur lors de l'exécution de l'outil %s", name)
        return {"error": f"Erreur lors de l'exécution de {name}: {err}"}
