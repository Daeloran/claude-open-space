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

Variables utiles : `OPENSPACE_TEAM` (noms séparés par des virgules), `OPENSPACE_CONTEXT` (taille de fenêtre utilisée pour la jauge de fatigue).

Le serveur écoute uniquement en local : les employés peuvent exécuter des commandes sur ta machine.

## Architecture

```
frontend/index.html   Rendu canvas pixel art, UI, moteur de démo (un seul fichier)
backend/app.py        FastAPI + WebSocket, un ClaudeSDKClient par employé
backend/events.py     Résumés lisibles des appels d'outils, détection des livrables
```

Le backend traduit les messages du SDK en événements de jeu. Le front ne connaît que ces événements, donc le rendu peut évoluer sans toucher au backend.

### Événements backend → front

`hello`, `snapshot`, `ticket_created`, `ticket_assigned`, `tool_use`, `tool_result`, `permission_request`, `permission_resolved`, `subagent_spawned`, `subagent_done`, `deliverable`, `context`, `compaction`, `cost`, `ticket_done`, `message`

À la connexion, `snapshot` suit `hello` avec l'état courant (tickets, totaux coût/tokens, fatigue par employé, validations en attente) : recharger l'onglet ou en ouvrir un second ne perd rien. `permission_resolved` ferme la validation sur tous les onglets.

### Messages front → backend

- `{"type": "new_ticket", "title": "..."}`
- `{"type": "permission_decision", "request_id": "...", "allow": true}`

## Pistes

- Vrais sprites (Aseprite) à la place du dessin procédural, animations de frappe au clavier
- Ambiance sonore et bruitages
- Un open space par dépôt, avec navigation entre les étages
- Tableau de bord de direction historisé (coût par jour, par employé)
- Recrutement : ajouter ou licencier un employé pendant la partie
- Vérifier les noms des champs du SDK selon la version installée (`total_cost_usd`, `usage`, `parent_tool_use_id`, sous-type `compact_boundary`)
