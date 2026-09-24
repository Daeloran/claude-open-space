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
| Session Claude Code lancée dans le terminal | Employé observé (sweat sombre, badge « terminal ») ; ses tickets sont tapés dans son onglet Konsole |

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

Les sessions Claude Code ouvertes dans ton terminal apparaissent aussi, comme employés observés : on voit leur projet, leur statut (tape au clavier / inactif), leurs outils en direct et leur fatigue, et on peut leur donner un ticket (voir ci-dessous). Toutes les 2 s, le backend lit `$CLAUDE_CONFIG_DIR/sessions/*.json` (seulement `pid`, `cwd`, `name`, `status`, `sessionId`, `entrypoint` ; sessions `cli` au pid vivant) et la fin de leur transcript `projects/*/<sessionId>.jsonl`. Les fichiers `*.key` et les champs de messagerie ne sont jamais lus ni envoyés au front. La taille de fenêtre n'étant pas dans le transcript, la fatigue la déduit du `model` de tes réglages Claude Code (`<projet>/.claude/settings.local.json`, puis `<projet>/.claude/settings.json`, puis `$CLAUDE_CONFIG_DIR/settings.json` ; `[1m]` → 1 M, sinon `OPENSPACE_CONTEXT`), lus à l'arrivée de la session ; en secours, 1 M si le modèle du transcript contient `[1m]` ou si le contexte dépasse la fenêtre. Un changement de modèle en cours de session (`/model`, réglages modifiés) n'est pris en compte qu'au prochain démarrage de la session ou de l'Open Space.

Un ticket adressé à un employé terminal (« Nom · projet · terminal » dans le sélecteur) est tapé dans son onglet Konsole puis validé, comme si tu l'avais saisi : la session le traite dans son propre contexte (pas de second processus sur la même session). Le backend retrouve l'onglet via `KONSOLE_DBUS_SERVICE` et `KONSOLE_DBUS_SESSION` dans `/proc/<pid>/environ` (seules ces deux variables sont lues, rien n'est journalisé), puis appelle `org.kde.konsole.Session.sendText` par `gdbus call` (à défaut `busctl --user call`), en sous-processus sans shell. Garde-fou : le texte n'est envoyé que si `foregroundProcessId()` de l'onglet est le pid de la session Claude ; sinon (session quittée, shell, autre programme au premier plan) rien n'est tapé et le ticket est refusé avec la raison. Les caractères de contrôle du ticket sont retirés (sauf retour à la ligne et tabulation) ; un ticket multi-ligne part en « bracketed paste » pour arriver en un seul message. Le ticket passe « fait » quand la session repasse inactive après avoir été occupée (statut du registre relevé toutes les 2 s : une réponse plus rapide que ça n'est pas vue, le ticket reste alors en cours jusqu'au prochain passage occupé → inactif, ou au départ de la session). Un seul ticket à la fois par session terminal. Limite : Konsole uniquement (pas tmux ni autres terminaux), et une session lancée hors Konsole reçoit `ticket_rejected`.

Le serveur écoute uniquement en local : les employés peuvent exécuter des commandes sur ta machine.

Le WebSocket `/ws` refuse toute connexion dont l'en-tête `Origin` n'est pas l'interface elle-même (`http://127.0.0.1:<port>`, `http://localhost:<port>` ou `http://[::1]:<port>`, même host et port que la requête) : une autre page ouverte dans ton navigateur ne peut ni créer de tickets ni valider de commandes. Derrière un proxy ou sur un autre port, ajoute les origines voulues via `OPENSPACE_ALLOWED_ORIGINS` (séparées par des virgules, ex. `OPENSPACE_ALLOWED_ORIGINS=http://localhost:3000`).

Les tests front (rendu de `frontend/index.html` en jsdom) tournent seulement si `OPENSPACE_JSDOM_PATH` pointe vers une install de jsdom (dossier contenant `node_modules/jsdom`) ; sinon ils sont ignorés (`skip`).

## Architecture

```
frontend/index.html   Rendu canvas pixel art, UI, moteur de démo (un seul fichier)
backend/app.py        FastAPI + WebSocket, un ClaudeSDKClient et une file de tickets par employé
backend/projects.py   Projets récents lus dans les transcripts Claude Code
backend/events.py     Résumés lisibles des appels d'outils, détection des livrables
backend/plan_usage.py Usage du plan (fenêtres 5 h et semaine)
backend/observer.py   Sessions du terminal observées (registre des sessions, suivi des transcripts)
backend/konsole.py    Envoi d'un prompt dans l'onglet Konsole d'une session terminal (D-Bus)
```

Le backend traduit les messages du SDK en événements de jeu. Le front ne connaît que ces événements, donc le rendu peut évoluer sans toucher au backend.

### Événements backend → front

`hello`, `snapshot`, `agent_hired`, `ticket_rejected`, `ticket_created`, `ticket_assigned`, `tool_use`, `tool_result`, `permission_request`, `permission_resolved`, `subagent_spawned`, `subagent_done`, `deliverable`, `context`, `compaction`, `cost`, `ticket_done`, `message`, `plan_usage`, `observed_joined`, `observed_left`, `observed_status`

`hello` : `{"team": [{"id", "name", "cwd", "project"}], "projects": [{"cwd", "name"}]}` (employés recrutés, projets proposés). `agent_hired` : `{"agent": {"id", "name", "cwd", "project"}}`. `ticket_rejected` : `{"title", "reason"}`, envoyé au seul onglet émetteur (employé inconnu, dossier introuvable ou ticket sans destination).

`observed_joined` : `{"agent": {"id": "o-<8 premiers caractères du sessionId>", "name", "cwd", "project", "status", "observed": true}}` (session du terminal). `observed_left` : `{"agent_id"}`. `observed_status` : `{"agent_id", "status"}` (`busy` / `idle`). Leur activité passe par les événements habituels (`tool_use`, `tool_result`, `context`). Un `new_ticket` adressé à un employé observé est tapé dans son onglet Konsole (`ticket_created` puis `ticket_assigned`, `ticket_done` quand il repasse `idle` après `busy`), ou reçoit `ticket_rejected` (onglet introuvable, Claude pas au premier plan, D-Bus indisponible, ticket déjà en cours).

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
