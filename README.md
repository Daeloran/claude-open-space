# L'Open Space

Une interface façon jeu de gestion pour piloter Claude Code. Chaque session Claude est un employé d'un open space en pixel art : il va aux archives quand il lit des fichiers, en salle serveur quand il lance une commande, au coin veille pour chercher sur le web, et vient frapper à ton bureau quand il a besoin d'une validation.

## Correspondances

| Dans Claude Code | Dans le jeu |
|---|---|
| Session `ClaudeSDKClient` | Employé (Léa, Hugo, Inès par défaut) |
| Prompt | Ticket client |
| `Read`, `Grep`, `Glob`, `LS` | Archives |
| `Edit`, `Write`, `MultiEdit` | Rédaction à son bureau |
| `Bash` | Salle serveur |
| `WebSearch`, `WebFetch` | Coin veille |
| `TodoWrite` | Tableau Kanban |
| `Task` (sous-agent) | Réunion avec un stagiaire |
| `can_use_tool` | L'employé vient à ton bureau, popup Autoriser / Refuser |
| Contexte qui grossit | Jauge de fatigue |
| Compaction du contexte | Pause café |
| Coût et tokens | Budget de l'entreprise |

## Lancer

Prérequis : Python 3.10+, Claude Code installé et authentifié (ou une clé `ANTHROPIC_API_KEY`, selon ton mode d'authentification).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
OPENSPACE_CWD=/chemin/du/repo/a/travailler uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Puis ouvre http://127.0.0.1:8000. Ajoute `?demo` à l'URL pour jouer avec des événements simulés, sans appeler Claude.

Variables utiles : `OPENSPACE_TEAM` (noms séparés par des virgules), `OPENSPACE_CONTEXT` (taille de fenêtre utilisée pour la jauge de fatigue), `OPENSPACE_PERMISSION_MODE` (optionnel, force un mode de permission ; par défaut les employés suivent tes réglages Claude Code : mode, règles allow, hooks, CLAUDE.md ; seules les permissions manquantes arrivent à ton bureau).

Le serveur écoute uniquement en local : les employés peuvent exécuter des commandes sur ta machine.

## Architecture

```
frontend/index.html   Rendu canvas pixel art, UI, moteur de démo (un seul fichier)
backend/app.py        FastAPI + WebSocket, un ClaudeSDKClient par employé
backend/events.py     Résumés lisibles des appels d'outils, détection des livrables
```

Le backend traduit les messages du SDK en événements de jeu. Le front ne connaît que ces événements, donc le rendu peut évoluer sans toucher au backend.

### Événements backend → front

`hello`, `ticket_created`, `ticket_assigned`, `tool_use`, `tool_result`, `permission_request`, `subagent_spawned`, `subagent_done`, `deliverable`, `context`, `compaction`, `cost`, `ticket_done`, `message`

### Messages front → backend

- `{"type": "new_ticket", "title": "..."}`
- `{"type": "permission_decision", "request_id": "...", "allow": true}`

## Pistes

- Vrais sprites (Aseprite) à la place du dessin procédural, animations de frappe au clavier
- Ambiance sonore et bruitages
- Un open space par dépôt, avec navigation entre les étages
- Tableau de bord de direction historisé (coût par jour, par employé)
- Recrutement : ajouter ou licencier un employé pendant la partie
