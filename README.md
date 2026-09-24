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

Un ticket adressé à un employé terminal (« Nom · projet · terminal » dans le sélecteur) est tapé dans son onglet Konsole puis validé, comme si tu l'avais saisi : la session le traite dans son propre contexte (pas de second processus sur la même session). Le backend retrouve l'onglet via `KONSOLE_DBUS_SERVICE` et `KONSOLE_DBUS_SESSION` dans `/proc/<pid>/environ` (seules ces deux variables sont lues, rien n'est journalisé), puis appelle `org.kde.konsole.Session.sendText` par `gdbus call` (à défaut `busctl --user call`), en sous-processus sans shell. Garde-fou : le texte n'est envoyé que si `foregroundProcessId()` de l'onglet est le pid de la session Claude ; sinon (session quittée, shell, autre programme au premier plan) rien n'est tapé et le ticket est refusé avec la raison. Les caractères de contrôle du ticket sont retirés (sauf retour à la ligne et tabulation) ; un ticket multi-ligne part en « bracketed paste » pour arriver en un seul message. Le ticket passe « fait » au premier des deux signaux : fin de réponse lue dans le transcript (message assistant principal avec `stop_reason: "end_turn"`, horodaté après l'envoi), ou retour de la session à inactive après avoir été occupée ; il échoue si la session part. Une réponse plus rapide que la relève du statut (2 s) est donc bien vue. Si Claude répondait encore à autre chose au moment de l'envoi, la fin de cette réponse-là peut clore le ticket un peu tôt. Un seul ticket à la fois par session terminal. Limite : Konsole uniquement (pas tmux ni autres terminaux), et une session lancée hors Konsole reçoit `ticket_rejected`.

Cliquer sur un employé, piloté ou terminal (dans l'open space ou sa ligne dans « Équipe »), le fait venir à ton bureau et ouvre un panneau de discussion (pour un piloté : le transcript de sa session SDK, `projects/*/<session_id>.jsonl`, disponible après son premier ticket ; ta réponse lui confie un ticket, après ses tickets en cours) : les 200 dernières entrées de sa session (tes prompts, ses réponses en Markdown avec coloration du code, ses appels d'outils repliés avec résumé et ✓/✗ ; ni réflexion, ni sous-agents, ni messages internes), puis ses nouvelles entrées en direct. Le champ en bas lui répond (Entrée envoie, Maj+Entrée va à la ligne) : c'est un ticket tapé dans son onglet Konsole comme ci-dessus, un refus s'affiche dans le panneau. « Fermer » ou Échap le renvoie à son bureau. Le contenu des transcripts ne passe que par le WebSocket, au seul onglet qui a ouvert le panneau, et n'est jamais journalisé ; les sorties d'outils sont tronquées à 2 000 caractères. Le rendu utilise marked, highlight.js et DOMPurify depuis cdnjs (versions épinglées, `integrity`) ; sans réseau, le panneau reste lisible en texte brut. En démo, « refacto-auth » a un faux historique et répond.

Prérequis : Konsole bloque `sendText` par défaut. Active « Enable the security sensitive parts of the DBus API » (Configurer Konsole → Général). Contrepartie : tout programme ayant accès à ton bus de session D-Bus peut alors taper dans tes onglets Konsole.

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

`hello`, `snapshot`, `agent_hired`, `ticket_rejected`, `ticket_created`, `ticket_assigned`, `tool_use`, `tool_result`, `permission_request`, `permission_resolved`, `subagent_spawned`, `subagent_done`, `deliverable`, `context`, `compaction`, `cost`, `ticket_done`, `message`, `plan_usage`, `observed_joined`, `observed_left`, `observed_status`, `observed_turn_end`, `chat_history`, `chat_entry`

`hello` : `{"team": [{"id", "name", "cwd", "project"}], "projects": [{"cwd", "name"}]}` (employés recrutés, projets proposés). `agent_hired` : `{"agent": {"id", "name", "cwd", "project"}}`. `ticket_rejected` : `{"title", "reason"}`, envoyé au seul onglet émetteur (employé inconnu, dossier introuvable ou ticket sans destination).

`observed_joined` : `{"agent": {"id": "o-<8 premiers caractères du sessionId>", "name", "cwd", "project", "status", "observed": true}}` (session du terminal). `observed_left` : `{"agent_id"}`. `observed_status` : `{"agent_id", "status"}` (`busy` / `idle` / `waiting`), avec `waiting_for` (champ `waitingFor` du registre, ex. `input needed`) seulement si `waiting`. Un employé terminal `waiting` (question, permission, dialogue) vient à ton bureau avec un « ! » rouge et n'accepte plus de ticket ; `idle`, il va au coin café ; `busy`, à sa place. `observed_turn_end` : `{"agent_id", "at"}`, fin d'une réponse de la session (horodatage du transcript), sert à clore son ticket ; le front l'ignore. Leur activité passe par les événements habituels (`tool_use`, `tool_result`, `context`). Un `new_ticket` adressé à un employé observé est tapé dans son onglet Konsole (`ticket_created` puis `ticket_assigned`, `ticket_done` quand il repasse `idle` après `busy`), ou reçoit `ticket_rejected` (onglet introuvable, Claude pas au premier plan, D-Bus indisponible, ticket déjà en cours).

`chat_history` : `{"agent_id", "entries": [...], "error"?}`, réponse à `open_chat` pour le seul onglet demandeur : 200 dernières entrées au plus, lues depuis la fin du transcript (4 Mo au plus) ; `entries: []` et `error` si l'employé est inconnu ou sans transcript. `chat_entry` : `{"agent_id", "entry"}`, chaque nouvelle entrée du transcript, aux seuls onglets dont le panneau est ouvert sur cet employé (hors `snapshot`). Entrée : `{"role": "user" | "assistant", "kind": "text" | "tool_use" | "tool_result", "ts"}` plus `text` (texte, ou sortie d'outil tronquée à 2 000 caractères), `tool` et `summary` (`tool_use`), `ok` (`tool_result`), `id` (relie un `tool_result` à son `tool_use`).

À la connexion, `snapshot` suit `hello` avec l'état courant (employés `agents`, tickets, totaux coût/tokens, fatigue par employé, validations en attente) : recharger l'onglet ou en ouvrir un second ne perd rien. `permission_resolved` ferme la validation sur tous les onglets.

Stagiaires : cliquer sur un stagiaire ouvre le panneau en lecture seule sur son sous-agent (`projects/<dossier>/<session>/subagents/agent-*.jsonl`, repéré par le `toolUseId` de son `.meta.json`) : sa consigne, ses outils, son rapport, en direct ; ceux d'une session terminal se déplacent aussi selon leurs outils. Limite : un sous-agent lancé en arrière-plan rend son résultat tout de suite, son stagiaire repart donc tôt.

`todos` : `{"agent_id", "todos": [{"content", "status"}]}`, dernière liste TodoWrite d'un employé (piloté ou terminal ; éléments invalides ignorés), rejouée dans le `snapshot` (`todos` : `{agent_id: [...]}`). Affichée dans le bandeau d'avancement du panneau (état, ticket, fatigue, tâches) et en « n/m tâches » dans « Équipe ».

`plan_usage` : `{"five_hour": {"utilization": 42, "resets_at": "<ISO 8601>"} | null, "seven_day": {...} | null}`, `utilization` en % (0-100). Envoyé à la connexion, toutes les 3 min et à chaque `RateLimitEvent` du SDK. Source : token OAuth de `$CLAUDE_CONFIG_DIR/.credentials.json` (défaut `~/.claude`) et endpoint non documenté `GET https://api.anthropic.com/api/oauth/usage` ; `null` (« — » dans le bandeau) si indisponible.

### Messages front → backend

- `{"type": "new_ticket", "title": "...", "agent_id": "e0"}` : ticket pour un employé existant
- `{"type": "new_ticket", "title": "...", "cwd": "/chemin/du/projet"}` : recrute un employé dans ce dossier et lui confie le ticket
- `{"type": "list_sessions"}` → `{"type": "sessions", "sessions": [{"session_id", "cwd", "project", "title", "updated", "live"?}]}` : conversations Claude Code reprenables (transcripts `projects/*/*.jsonl`, plus récentes d'abord, titre = premier prompt ; `live` si un processus du terminal la tient).
- `{"type": "resume", "session_id": "..."}` : recrute un employé piloté qui reprend cette conversation (`resume` du SDK, dans son dossier ; `agent_hired` avec `resumed`). Refusée (`resume_rejected` `{"session_id", "reason"}`) si l'id est invalide, la session introuvable, déjà reprise, ou encore ouverte dans le terminal : quitte Claude dans Konsole d'abord, jamais deux processus sur une session. Dans le jeu : destination « Reprendre une conversation… » du ticket.
- `{"type": "set_mode", "agent_id": "e0", "mode": "plan"}` : mode de permission d'un employé piloté (`default`, `acceptEdits`, `plan`, `auto`, `bypassPermissions` ; ce dernier demande confirmation dans le jeu), appliqué à sa session et gardé pour la suite → `mode_changed` `{"agent_id", "mode"}` (aussi dans `hello`/`snapshot` : `mode`, `null` = tes réglages Claude Code) ; mode inconnu ou employé terminal → `mode_rejected` `{"agent_id", "reason"}`. En mode plan, le plan (`ExitPlanMode`) arrive à ton bureau (`permission_request` avec `plan`) pour être approuvé ou refusé.
- `{"type": "interrupt", "agent_id": "e0"}` : interrompt la réponse en cours d'un employé piloté (équivalent d'Échap ; ticket `ticket_done` `ok: false`, `reason: "interrompu"`, demande de permission en attente abandonnée, session gardée). Employé terminal : `interrupt_rejected` `{"agent_id", "reason"}`.
- `{"type": "permission_decision", "request_id": "...", "allow": true}` ; pour une question (`AskUserQuestion` d'un employé piloté, dont le `permission_request` porte `questions` : `[{"question", "header", "multi", "options": [{"label", "description"}]}]`), `answers` : `{"<texte de la question>": "<libellé(s) joints par « , » ou texte libre>"}`. Réponses à des questions inconnues ou non textuelles ignorées, 4 000 caractères max. `allow: false` = « Ignorer » : Claude est prévenu que tu n'as pas répondu.
- `{"type": "open_chat", "agent_id": "o-..."}` : historique (`chat_history`) puis entrées en direct (`chat_entry`) de cet employé terminal
- `{"type": "close_chat", "agent_id": "o-..."}` : arrête l'envoi des entrées en direct (aussi à la déconnexion)

## Pistes

- Vrais sprites (Aseprite) à la place du dessin procédural, animations de frappe au clavier
- Ambiance sonore et bruitages
- Un open space par dépôt, avec navigation entre les étages
- Tableau de bord de direction historisé (coût par jour, par employé)
- Licencier un employé, reprendre une session passée (`resume`)
