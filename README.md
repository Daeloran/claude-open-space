# L'Open Space

Une interface façon jeu de gestion pour piloter Claude Code. Chaque session Claude est un employé d'un open space en pixel art : il va aux archives quand il lit des fichiers, en salle serveur quand il lance une commande, au coin veille pour chercher sur le web, et vient frapper à ton bureau quand il a besoin d'une validation.

## Correspondances

| Dans Claude Code | Dans le jeu |
|---|---|
| Session `ClaudeSDKClient` (dans un dossier) | Employé recruté sur un projet |
| Prompt | Ticket client, adressé à un employé |
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
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Puis ouvre http://127.0.0.1:8000. Ajoute `?demo` à l'URL pour jouer avec des événements simulés, sans appeler Claude.

L'open space démarre vide. À chaque ticket, tu choisis sa destination : un employé existant (le ticket suit dans sa session, après ses tickets en cours) ou « Nouvel employé… » sur un projet. Les projets proposés sont les dossiers récents de tes sessions Claude Code (champ `cwd` des transcripts `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`, défaut `~/.claude`), ou un autre dossier saisi à la main.

Variables utiles : `OPENSPACE_CWD` (projet proposé en tête de liste), `OPENSPACE_TEAM` (prénoms des recrues, séparés par des virgules ; réutilisés avec un numéro une fois épuisés, défaut `Léa,Hugo,Inès`), `OPENSPACE_CONTEXT` (taille de fenêtre de repli pour la jauge de fatigue, si la session ne la fournit pas), `OPENSPACE_PERMISSION_MODE` (optionnel, force un mode de permission ; par défaut les employés suivent tes réglages Claude Code : mode, règles allow, hooks, CLAUDE.md ; seules les permissions manquantes arrivent à ton bureau).

Le serveur écoute uniquement en local : les employés peuvent exécuter des commandes sur ta machine.

Le WebSocket `/ws` refuse toute connexion dont l'en-tête `Origin` n'est pas l'interface elle-même (`http://127.0.0.1:<port>`, `http://localhost:<port>` ou `http://[::1]:<port>`, même host et port que la requête) : une autre page ouverte dans ton navigateur ne peut ni créer de tickets ni valider de commandes. Derrière un proxy ou sur un autre port, ajoute les origines voulues via `OPENSPACE_ALLOWED_ORIGINS` (séparées par des virgules, ex. `OPENSPACE_ALLOWED_ORIGINS=http://localhost:3000`).

## Architecture

```
frontend/index.html   Rendu canvas pixel art, UI, moteur de démo (un seul fichier)
backend/app.py        FastAPI + WebSocket, un ClaudeSDKClient et une file de tickets par employé
backend/projects.py   Projets récents lus dans les transcripts Claude Code
backend/events.py     Résumés lisibles des appels d'outils, détection des livrables
backend/plan_usage.py Usage du plan (fenêtres 5 h et semaine)
```

Le backend traduit les messages du SDK en événements de jeu. Le front ne connaît que ces événements, donc le rendu peut évoluer sans toucher au backend.

### Événements backend → front

`hello`, `snapshot`, `agent_hired`, `ticket_rejected`, `ticket_created`, `ticket_assigned`, `tool_use`, `tool_result`, `permission_request`, `permission_resolved`, `subagent_spawned`, `subagent_done`, `deliverable`, `context`, `compaction`, `cost`, `ticket_done`, `message`, `plan_usage`

`hello` : `{"team": [{"id", "name", "cwd", "project"}], "projects": [{"cwd", "name"}]}` (employés recrutés, projets proposés). `agent_hired` : `{"agent": {"id", "name", "cwd", "project"}}`. `ticket_rejected` : `{"title", "reason"}`, envoyé au seul onglet émetteur (employé inconnu, dossier introuvable ou ticket sans destination).

À la connexion, `snapshot` suit `hello` avec l'état courant (employés `agents`, tickets, totaux coût/tokens, fatigue par employé, validations en attente) : recharger l'onglet ou en ouvrir un second ne perd rien. `permission_resolved` ferme la validation sur tous les onglets.

`plan_usage` : `{"five_hour": {"utilization": 42, "resets_at": "<ISO 8601>"} | null, "seven_day": {...} | null}`, `utilization` en % (0-100). Envoyé à la connexion, toutes les 3 min et à chaque `RateLimitEvent` du SDK. Source : token OAuth de `$CLAUDE_CONFIG_DIR/.credentials.json` (défaut `~/.claude`) et endpoint non documenté `GET https://api.anthropic.com/api/oauth/usage` ; `null` (« — » dans le bandeau) si indisponible.

### Messages front → backend

- `{"type": "new_ticket", "title": "...", "agent_id": "e0"}` : ticket pour un employé existant
- `{"type": "new_ticket", "title": "...", "cwd": "/chemin/du/projet"}` : recrute un employé dans ce dossier et lui confie le ticket
- `{"type": "permission_decision", "request_id": "...", "allow": true}`

## Pistes

- Vrais sprites (Aseprite) à la place du dessin procédural, animations de frappe au clavier
- Ambiance sonore et bruitages
- Un open space par dépôt, avec navigation entre les étages
- Tableau de bord de direction historisé (coût par jour, par employé)
- Licencier un employé, reprendre une session passée (`resume`)
