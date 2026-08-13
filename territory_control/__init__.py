from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import os
import json
import logging
import threading
from urllib.request import Request, urlopen

import qrcode
from flask import abort, jsonify, request, render_template_string, send_file
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from CTFd.models import Awards, Challenges, Solves, Teams, db
from CTFd.plugins import bypass_csrf_protection
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge
from CTFd.utils.user import get_current_team
from CTFd.utils.decorators import admins_only

from .models import CaptureSession, DeviceCommand, TeamIdentity, Territory, TerritoryAttack, TerritoryChallenge, TerritorySetting


BLACK = "000000"
CAPTURE_WINDOW_SECONDS = 30
TELEGRAM_API = "https://ta.de.snnlab.ru"
logger = logging.getLogger(__name__)
telegram_lock = threading.Lock()
telegram_pending = []
telegram_timer = None


def points(value, field="value"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result.quantize(Decimal("0.0001"))


def configured_points(name, default):
    return points(os.getenv(name, default), name)


def team_color(team_id):
    # Stable high-contrast colors make a team recognizable without requiring CTFd theme changes.
    digest = sha256(str(team_id).encode()).hexdigest()
    return f"{int(digest[:6], 16) | 0x404040:06x}"


def color(value):
    value = str(value or "").strip().lstrip("#")
    if len(value) != 6 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError("color must be a six-digit RGB hex value")
    return value.lower()


def canonical_uuid(value):
    value = str(value or "").replace("-", "").lower()
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        return None
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


def device_authorized():
    secret = os.getenv("TERRITORY_DEVICE_SECRET")
    # Some CTFd proxy/WSGI stacks expose custom headers only through environ.
    supplied = request.headers.get("X-Territory-Secret") or request.environ.get("HTTP_X_TERRITORY_SECRET")
    return bool(secret and supplied == secret)


def owner_color(territory):
    return identity_for(territory.owner_team_id).color if territory.owner_team_id else BLACK


def attack_record(territory, team_id, attack_points, prior_defense, result, note=None):
    db.session.add(TerritoryAttack(
        territory_id=territory.id,
        team_id=team_id,
        attack_points=attack_points,
        prior_defense_points=prior_defense,
        defense_points=territory.defense_points,
        result=result,
        note=note,
    ))


def setting_value(key, default=""):
    setting = TerritorySetting.query.get(key)
    return setting.value if setting else default


def send_telegram_message(text, token, recipients):
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    for chat_id in recipients:
        try:
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
            request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=4) as response:
                if response.status < 200 or response.status >= 300:
                    logger.warning("Telegram returned HTTP %s", response.status)
        except Exception:
            logger.exception("Telegram notification failed for configured recipient")


def flush_telegram_messages():
    global telegram_timer
    with telegram_lock:
        pending = telegram_pending[:]
        telegram_pending.clear()
        telegram_timer = None
    if not pending:
        return
    token, recipients, messages = pending[0][0], pending[0][1], [item[2] for item in pending]
    send_telegram_message("Territory updates\n\n" + "\n\n".join(messages), token, recipients)


def queue_telegram_message(text):
    global telegram_timer
    token = setting_value("telegram_bot_token").strip()
    recipients = [item.strip() for item in setting_value("telegram_recipient_ids").replace(",", "\n").splitlines() if item.strip()]
    if not token or not recipients:
        return
    with telegram_lock:
        telegram_pending.append((token, recipients, text))
        if telegram_timer is None:
            telegram_timer = threading.Timer(20, flush_telegram_messages)
            telegram_timer.daemon = True
            telegram_timer.start()


def team_status_rows(teams, territories):
    identities = {identity.team_id: identity for identity in TeamIdentity.query.all()}
    captured = {}
    for territory in territories:
        if territory.owner_team_id:
            captured.setdefault(territory.owner_team_id, []).append(territory.name)
    return [
        {
            "id": team.id,
            "name": team.name,
            "attack_points": str(identities.get(team.id).attack_points) if team.id in identities else "0",
            "captured": captured.get(team.id, []),
        }
        for team in teams
    ]


def upgrade_schema():
    """Small additive migration for deployments created before capture timing existed."""
    columns = {column["name"] for column in inspect(db.engine).get_columns("territory_control_territories")}
    for name in ("captured_at", "last_seen_at"):
        if name not in columns:
            db.session.execute(text(f"ALTER TABLE territory_control_territories ADD COLUMN {name} DATETIME"))
        db.session.commit()


def identity_for(team_id, lock=False):
    query = TeamIdentity.query.filter_by(team_id=team_id)
    if lock:
        query = query.with_for_update()
    identity = query.first()
    if identity is None:
        identity = TeamIdentity(team_id=team_id, color=team_color(team_id))
        db.session.add(identity)
        db.session.flush()
    return identity


def current_team_or_403():
    team = get_current_team()
    if team is None:
        abort(403, "Join a team before attacking a territory")
    return team


def expire_session(session):
    if session.status == "pending" and session.expires_at <= datetime.utcnow():
        session.status = "expired"
        session.completed_at = datetime.utcnow()
        identity = identity_for(session.team_id, lock=True)
        identity.attack_points += session.attack_points


class TerritoryControlChallenge(CTFdStandardChallenge):
    id = "territory"
    name = "Territory Attack Points"
    challenge_model = TerritoryChallenge
    templates = {
        "create": "/plugins/territory_control/assets/challenge-create.html",
        "update": "/plugins/territory_control/assets/challenge-update.html",
        "view": "/plugins/territory_control/assets/challenge-view.html",
    }
    # CTFd's API requires create/update/view keys even when the templates
    # inherit the standard challenge views.
    scripts = {
        "create": "/plugins/challenges/assets/create.js",
        "update": "/plugins/challenges/assets/update.js",
        "view": "/plugins/challenges/assets/view.js",
    }

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json() or {}
        attack_points = points(data.get("attack_points", 0), "attack_points")
        # Challenges remain worth zero in CTFd; only plugin balance receives points on solve.
        mutable = dict(data)
        mutable["value"] = 0
        mutable["attack_points"] = attack_points
        # CTFd's challenge modal embeds this as JavaScript; NULL renders invalid code.
        mutable["max_attempts"] = mutable.get("max_attempts") or 0
        # BaseChallenge reads the request itself. Instantiate directly to avoid mutating Flask input.
        challenge = cls.challenge_model(**mutable)
        db.session.add(challenge)
        db.session.commit()
        return challenge

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data["attack_points"] = str(challenge.attack_points)
        return data

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json() or {}
        if "attack_points" in data:
            challenge.attack_points = points(data["attack_points"], "attack_points")
        # A challenge may never be changed into an official-score source.
        challenge.value = 0
        for key, value in data.items():
            if key not in {"attack_points", "value", "id", "type"}:
                setattr(challenge, key, value)
        db.session.commit()
        return challenge

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)
        if team:
            identity = identity_for(team.id, lock=True)
            identity.attack_points += challenge.attack_points
            db.session.commit()


def load(app):
    from CTFd.plugins import (
        register_plugin_assets_directory,
        register_plugin_script,
        register_user_page_menu_bar,
    )

    app.db.create_all()
    upgrade_schema()
    CHALLENGE_CLASSES[TerritoryControlChallenge.id] = TerritoryControlChallenge
    register_plugin_assets_directory(app, base_path="/plugins/territory_control/assets/")
    register_plugin_script("/plugins/territory_control/assets/profile-ui.js")
    register_user_page_menu_bar("Territory Control", "/territory-control")

    @app.get("/api/v1/territory-control/me")
    def territory_me():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        return jsonify(
            uuid=identity.uuid,
            qr_uri=f"ss-forest://{identity.uuid}",
            color=identity.color,
            attack_points=str(identity.attack_points),
        )

    @app.get("/api/v1/territory-control/teams/<int:team_id>/points")
    def territory_team_points(team_id):
        """Public scoreboard companion for a CTFd team profile."""
        team = Teams.query.get_or_404(team_id)
        identity = TeamIdentity.query.filter_by(team_id=team.id).first()
        solve_points = db.session.query(db.func.coalesce(db.func.sum(Challenges.value), 0)).join(
            Solves, Solves.challenge_id == Challenges.id
        ).filter(Solves.team_id == team.id).scalar()
        award_points = db.session.query(db.func.coalesce(db.func.sum(Awards.value), 0)).filter(
            Awards.team_id == team.id
        ).scalar()
        return jsonify(
            attack_points=str(identity.attack_points) if identity else "0",
            score=int(solve_points or 0) + int(award_points or 0),
        )

    @app.get("/api/v1/territory-control/challenge-rewards")
    def territory_challenge_rewards():
        return jsonify({
            challenge.id: str(challenge.attack_points)
            for challenge in TerritoryChallenge.query.all()
        })

    @app.get("/api/v1/territory-control/me/qr")
    def territory_qr():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        image = qrcode.make(f"ss-forest://{identity.uuid}")
        output = BytesIO()
        image.save(output, "PNG")
        output.seek(0)
        return send_file(output, mimetype="image/png", download_name="team-uuid.png")

    @app.get("/territory-control")
    def territory_player_page():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        territories = Territory.query.order_by(Territory.name).all()
        teams = Teams.query.order_by(Teams.name).all()
        owners = {team.id: team.name for team in teams}
        owner_colors = {
            territory.owner_team_id: owner_color(territory)
            for territory in territories if territory.owner_team_id
        }
        return render_template_string(
            PLAYER_TEMPLATE,
            attack_points=str(identity.attack_points),
            team_uuid=identity.uuid,
            team_color=identity.color,
            territories=territories,
            owners=owners,
            owner_colors=owner_colors,
            now=datetime.utcnow(),
            availability_seconds=int(os.getenv("TERRITORY_NODE_STALE_SECONDS", "90")),
            team_statuses=team_status_rows(teams, territories),
        )

    @app.post("/api/v1/territory-control/me/color")
    @bypass_csrf_protection
    def update_team_color():
        team = current_team_or_403()
        data = request.get_json(silent=True) or request.form
        try:
            selected_color = color(data.get("color"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        identity = identity_for(team.id, lock=True)
        identity.color = selected_color
        db.session.commit()
        return jsonify(color=selected_color)

    @app.post("/api/v1/territory-control/device/attacks")
    @bypass_csrf_protection
    def device_attack():
        """Resolve one physical UUID_REQUEST from a trusted serial worker."""
        if not device_authorized():
            abort(403)
        data = request.get_json(silent=True) or {}
        try:
            attack_points = points(data.get("attack_points"), "attack_points")
        except ValueError as error:
            return jsonify(action="status", status="INVALID_ATTACK_POINTS", error=str(error)), 400
        if attack_points <= 0:
            return jsonify(action="status", status="INVALID_ATTACK_POINTS", error="attack_points must be greater than zero"), 400
        territory = Territory.query.filter_by(node_id=str(data.get("node_id", "")).lower()).with_for_update().first()
        if territory is None:
            return jsonify(action="status", status="UNKNOWN_TERRITORY", error="unknown territory"), 404
        scanned_uuid = canonical_uuid(data.get("uuid"))
        scanned = TeamIdentity.query.filter_by(uuid=scanned_uuid).with_for_update().first() if scanned_uuid else None
        if scanned is None:
            return jsonify(action="status", status="INVALID_TEAM_UUID", error="unknown team UUID"), 403
        identity = identity_for(scanned.team_id, lock=True)
        if identity.attack_points < attack_points:
            return jsonify(action="status", status="NOT_ENOUGH_POINTS_TO_CAPTURE", error="insufficient attack points"), 409
        identity.attack_points -= attack_points
        defense_multiplier = configured_points("TERRITORY_DEFENSE_MULTIPLIER", "1")
        attack_multiplier = configured_points("TERRITORY_ATTACK_MULTIPLIER", "2")
        prior_defense = territory.defense_points
        prior_owner = Teams.query.get(territory.owner_team_id) if territory.owner_team_id else None
        remaining = (prior_defense * defense_multiplier) - (attack_points * attack_multiplier)
        if remaining > 0:
            territory.defense_points = remaining
            result = "defended"
            response_color = owner_color(territory)
        elif remaining == 0:
            territory.owner_team_id = None
            territory.defense_points = Decimal("0")
            territory.captured_at = None
            result = "neutralized"
            response_color = BLACK
        else:
            territory.owner_team_id = scanned.team_id
            territory.defense_points = abs(remaining)
            territory.last_awarded_at = datetime.utcnow()
            territory.captured_at = datetime.utcnow()
            result = "captured"
            response_color = scanned.color
        attack_record(territory, scanned.team_id, attack_points, prior_defense, result)
        db.session.commit()
        attacking_team = Teams.query.get(scanned.team_id)
        attacking_name = attacking_team.name if attacking_team else "Deleted team"
        prior_owner_name = prior_owner.name if prior_owner else "Neutral"
        if result == "captured":
            notification = f"{attacking_name} captured node from {prior_owner_name}\nMAC: {territory.node_id}"
        else:
            notification = f"{attacking_name} tried to attack {prior_owner_name}\nMAC: {territory.node_id}"
        queue_telegram_message(notification)
        return jsonify(action="color", color=response_color, result=result, defense_points=str(territory.defense_points))

    @app.post("/api/v1/territory-control/device/topology")
    @bypass_csrf_protection
    def device_topology():
        """Update territory availability from a complete root `tree` snapshot."""
        if not device_authorized():
            abort(403)
        data = request.get_json(silent=True) or {}
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(node, str) for node in nodes):
            return jsonify(error="nodes must be a list of node MACs"), 400
        now = datetime.utcnow()
        normalized = {node.lower() for node in nodes}
        territories = Territory.query.filter(Territory.node_id.in_(normalized)).with_for_update().all()
        known = {territory.node_id for territory in territories}
        for node_id in normalized - known:
            territory = Territory(
                node_id=node_id,
                name=f"Node {node_id}",
                defense_points=Decimal("0"),
                score_amount=Decimal("1"),
                score_interval_seconds=10,
                last_seen_at=now,
            )
            db.session.add(territory)
            territories.append(territory)
        for territory in territories:
            territory.last_seen_at = now
        db.session.commit()
        return jsonify(created=len(normalized - known), updated=len(territories), observed_at=now.isoformat())

    @app.route("/admin/territory-control", methods=["GET", "POST"])
    @admins_only
    def territory_admin():
        if request.method == "POST":
            try:
                territory = Territory(
                    node_id=request.form["node_id"].strip().lower(),
                    name=request.form["name"].strip(),
                    defense_points=points(request.form.get("defense_points", "1"), "defense_points"),
                    score_amount=points(request.form.get("score_amount", "0"), "score_amount"),
                    score_interval_seconds=int(request.form.get("score_interval_seconds", 300)),
                )
                if territory.defense_points <= 0 or territory.score_interval_seconds <= 0:
                    raise ValueError("defense and interval must be greater than zero")
                if territory.score_amount != territory.score_amount.to_integral_value():
                    raise ValueError("score amount must be a whole number because CTFd Awards only support integers")
                db.session.add(territory)
                db.session.commit()
            except (KeyError, ValueError, IntegrityError) as error:
                db.session.rollback()
                teams = Teams.query.order_by(Teams.name).all()
                territories = Territory.query.all()
                return render_template_string(ADMIN_TEMPLATE, territories=territories, teams=teams, team_names={team.id: team.name for team in teams}, team_statuses=team_status_rows(teams, territories), attacks=[], telegram_bot_token=setting_value("telegram_bot_token"), telegram_recipient_ids=setting_value("telegram_recipient_ids"), error=str(error)), 400
        territories = Territory.query.order_by(Territory.name).all()
        attacks = TerritoryAttack.query.order_by(TerritoryAttack.created_at.desc()).limit(100).all()
        teams = Teams.query.order_by(Teams.name).all()
        return render_template_string(ADMIN_TEMPLATE, territories=territories, teams=teams, team_names={team.id: team.name for team in teams}, team_statuses=team_status_rows(teams, territories), attacks=attacks, telegram_bot_token=setting_value("telegram_bot_token"), telegram_recipient_ids=setting_value("telegram_recipient_ids"), error=None)

    @app.post("/admin/territory-control/telegram")
    @admins_only
    @bypass_csrf_protection
    def update_telegram_settings():
        for key, form_key in (("telegram_bot_token", "bot_token"), ("telegram_recipient_ids", "recipient_ids")):
            setting = TerritorySetting.query.get(key) or TerritorySetting(key=key)
            setting.value = request.form.get(form_key, "").strip()
            db.session.add(setting)
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/admin/territory-control/territories/<int:territory_id>")
    @admins_only
    @bypass_csrf_protection
    def update_territory(territory_id):
        territory = Territory.query.filter_by(id=territory_id).with_for_update().first_or_404()
        try:
            prior_defense = territory.defense_points
            territory.name = request.form["name"].strip()
            territory.node_id = request.form["node_id"].strip().lower()
            territory.defense_points = points(request.form["defense_points"], "defense_points")
            territory.score_amount = points(request.form["score_amount"], "score_amount")
            territory.score_interval_seconds = int(request.form["score_interval_seconds"])
            if not territory.name or territory.score_interval_seconds <= 0:
                raise ValueError("name and interval must be greater than zero")
            if territory.score_amount != territory.score_amount.to_integral_value():
                raise ValueError("score amount must be a whole number")
            attack_record(territory, None, Decimal("0"), prior_defense, "admin_updated", "Territory settings updated")
            db.session.commit()
        except (KeyError, ValueError, IntegrityError) as error:
            db.session.rollback()
            abort(400, str(error))
        return jsonify(ok=True)

    @app.post("/admin/territory-control/territories/<int:territory_id>/owner")
    @admins_only
    @bypass_csrf_protection
    def force_owner(territory_id):
        territory = Territory.query.filter_by(id=territory_id).with_for_update().first_or_404()
        prior_defense = territory.defense_points
        owner_team_id = request.form.get("owner_team_id", type=int)
        if owner_team_id and Teams.query.get(owner_team_id) is None:
            abort(404, "unknown team")
        territory.owner_team_id = owner_team_id
        territory.captured_at = datetime.utcnow() if owner_team_id else None
        territory.last_awarded_at = datetime.utcnow() if owner_team_id else None
        attack_record(territory, owner_team_id, Decimal("0"), prior_defense, "admin_owner_change", "Ownership changed by admin")
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/admin/territory-control/teams/<int:team_id>/attack-points")
    @admins_only
    @bypass_csrf_protection
    def adjust_attack_points(team_id):
        if Teams.query.get(team_id) is None:
            abort(404, "unknown team")
        try:
            delta = Decimal(str(request.form["delta"])).quantize(Decimal("0.0001"))
        except (KeyError, InvalidOperation, ValueError):
            abort(400, "delta must be a number")
        identity = identity_for(team_id, lock=True)
        if identity.attack_points + delta < 0:
            abort(400, "adjustment cannot make AP negative")
        identity.attack_points += delta
        db.session.commit()
        return jsonify(ok=True, attack_points=str(identity.attack_points))

    @app.post("/admin/territory-control/challenges/<int:challenge_id>/convert")
    @admins_only
    def convert_challenge(challenge_id):
        """Convert an unsolved standard challenge created before the type-template fix."""
        challenge = Challenges.query.filter_by(id=challenge_id, type="standard").with_for_update().first_or_404()
        if Solves.query.filter_by(challenge_id=challenge.id).count():
            return jsonify(error="Solved challenges cannot be converted safely"), 409
        try:
            attack_points = points(request.form.get("attack_points"), "attack_points")
        except ValueError as error:
            return jsonify(error=str(error)), 400
        challenge.type = "territory"
        challenge.value = 0
        db.session.add(TerritoryChallenge(id=challenge.id, attack_points=attack_points))
        db.session.commit()
        return jsonify(id=challenge.id, type="territory", attack_points=str(attack_points))


ADMIN_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Territory Control Admin</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="min-h-screen bg-slate-950 text-slate-100"><main class="mx-auto max-w-7xl px-4 py-10 sm:px-6"><header class="mb-8 flex flex-col gap-4 border-b border-slate-800 pb-7 sm:flex-row sm:items-end sm:justify-between"><div><p class="text-xs font-bold uppercase tracking-[.24em] text-emerald-400">Operations Console</p><h1 class="mt-2 text-3xl font-semibold tracking-tight">Territory Control</h1><p class="mt-2 max-w-2xl text-sm text-slate-400">Nodes are discovered automatically from root <code class="rounded bg-slate-800 px-1.5 py-0.5 text-slate-200">tree</code> snapshots. New nodes award 1 CTFd point every 10 seconds.</p></div><a class="rounded-lg border border-slate-700 px-4 py-2 text-sm font-medium text-slate-200 hover:bg-slate-800" href="/territory-control">Player view</a></header>
{% if error %}<div class="mb-6 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{{ error }}</div>{% endif %}
<section class="mb-10"><div class="mb-4 flex items-center justify-between"><h2 class="text-lg font-semibold">Territories</h2><span class="rounded-full bg-slate-800 px-3 py-1 text-xs text-slate-400">{{ territories|length }} discovered</span></div><div class="overflow-hidden rounded-xl border border-slate-800 bg-slate-900 shadow-2xl shadow-black/20"><div class="overflow-x-auto"><table class="min-w-full text-left text-sm"><thead class="bg-slate-800/70 text-xs uppercase tracking-wider text-slate-400"><tr><th class="px-5 py-4">Territory</th><th class="px-5 py-4">Ownership</th><th class="px-5 py-4">Defense / award</th><th class="px-5 py-4">Configuration</th></tr></thead><tbody class="divide-y divide-slate-800">{% for territory in territories %}<tr class="align-top hover:bg-slate-800/30"><td class="px-5 py-4"><p class="font-medium text-white">{{ territory.name }}</p><p class="mt-1 font-mono text-xs text-slate-500">{{ territory.node_id }}</p></td><td class="px-5 py-4">{% if territory.owner_team_id %}<p class="font-medium text-emerald-300">Captured</p><p class="mt-1 text-slate-400">{{ team_names.get(territory.owner_team_id, 'Deleted team') }}</p>{% else %}<span class="rounded-full bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-400">Neutral</span>{% endif %}<form class="mt-3 flex gap-2" method="post" action="/admin/territory-control/territories/{{ territory.id }}/owner"><select class="min-w-0 rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200" name="owner_team_id"><option value="">Set neutral</option>{% for team in teams %}<option value="{{ team.id }}" {% if team.id == territory.owner_team_id %}selected{% endif %}>{{ team.name }}</option>{% endfor %}</select><button class="rounded-md bg-slate-700 px-2 py-1.5 text-xs font-medium hover:bg-slate-600">Apply</button></form></td><td class="px-5 py-4"><p class="font-mono text-slate-200">{{ territory.defense_points }} defense</p><p class="mt-1 text-xs text-slate-500">{{ territory.score_amount }} CTFd / {{ territory.score_interval_seconds }}s</p></td><td class="px-5 py-4"><form class="grid min-w-[32rem] grid-cols-5 gap-2" method="post" action="/admin/territory-control/territories/{{ territory.id }}"><input class="rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" name="name" value="{{ territory.name }}" required><input class="rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs" name="node_id" value="{{ territory.node_id }}" required><input class="rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" name="defense_points" value="{{ territory.defense_points }}" type="number" min="0" step="0.0001" required><input class="rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" name="score_amount" value="{{ territory.score_amount }}" type="number" min="0" step="1" required><div class="flex gap-2"><input class="min-w-0 rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" name="score_interval_seconds" value="{{ territory.score_interval_seconds }}" type="number" min="1" required><button class="rounded-md bg-emerald-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-emerald-400">Save</button></div></form></td></tr>{% else %}<tr><td class="px-5 py-8 text-center text-slate-500" colspan="4">Waiting for a root tree snapshot.</td></tr>{% endfor %}</tbody></table></div></div></section>
<section class="grid gap-8 xl:grid-cols-[1.2fr_.8fr]"><div><h2 class="mb-4 text-lg font-semibold">Teams</h2><div class="overflow-hidden rounded-xl border border-slate-800 bg-slate-900"><div class="overflow-x-auto"><table class="min-w-full text-left text-sm"><thead class="bg-slate-800/70 text-xs uppercase tracking-wider text-slate-400"><tr><th class="px-5 py-4">Team</th><th class="px-5 py-4">Attack points</th><th class="px-5 py-4">Captured territories</th><th class="px-5 py-4">Adjust AP</th></tr></thead><tbody class="divide-y divide-slate-800">{% for status in team_statuses %}<tr><td class="px-5 py-4 font-medium text-white">{{ status.name }}</td><td class="px-5 py-4 font-mono text-emerald-300">{{ status.attack_points }}</td><td class="px-5 py-4 text-slate-400">{% if status.captured %}{{ status.captured|join(', ') }}{% else %}<span class="text-slate-600">None</span>{% endif %}</td><td class="px-5 py-4"><form class="flex gap-2" method="post" action="/admin/territory-control/teams/{{ status.id }}/attack-points"><input class="w-24 rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" name="delta" type="number" step="0.0001" placeholder="+/- AP" required><button class="rounded-md bg-slate-700 px-3 py-1.5 text-xs font-medium hover:bg-slate-600">Apply</button></form></td></tr>{% endfor %}</tbody></table></div></div></div>
<div><h2 class="mb-4 text-lg font-semibold">Recent events</h2><div class="overflow-hidden rounded-xl border border-slate-800 bg-slate-900"><div class="max-h-[34rem] overflow-y-auto divide-y divide-slate-800">{% for attack in attacks %}<article class="px-5 py-4"><div class="flex items-center justify-between gap-3"><p class="font-medium text-slate-200">{{ attack.result|replace('_', ' ')|title }}</p><span class="font-mono text-xs text-slate-500">{{ attack.attack_points }} AP</span></div><p class="mt-1 text-sm text-slate-400">{{ team_names.get(attack.team_id, 'Admin') }} on territory #{{ attack.territory_id }}</p><p class="mt-1 text-xs text-slate-600">{{ attack.created_at }}{% if attack.note %} · {{ attack.note }}{% endif %}</p></article>{% else %}<p class="px-5 py-8 text-sm text-slate-500">No events recorded yet.</p>{% endfor %}</div></div></div></section>
<section class="mt-8 rounded-xl border border-slate-800 bg-slate-900 p-5"><h2 class="text-lg font-semibold">Telegram notifications</h2><p class="mt-1 text-sm text-slate-400">Uses <code>https://ta.de.snnlab.ru</code>. Enter one Telegram user or channel ID per line.</p><form class="mt-4 grid gap-4 md:grid-cols-2" method="post" action="/admin/territory-control/telegram"><label class="text-sm text-slate-300">Bot token<input class="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm" name="bot_token" type="password" value="{{ telegram_bot_token }}" autocomplete="off"></label><label class="text-sm text-slate-300">User/channel IDs<textarea class="mt-1 min-h-24 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm" name="recipient_ids" placeholder="123456789\n-1001234567890">{{ telegram_recipient_ids }}</textarea></label><div><button class="rounded-md bg-emerald-500 px-4 py-2 text-sm font-bold text-slate-950 hover:bg-emerald-400">Save Telegram settings</button></div></form></section></main></body></html>"""


PLAYER_TEMPLATE = """<!doctype html>
<title>Territory Control</title>
<style>
body { max-width: 960px; margin: 2rem auto; font: 16px system-ui, sans-serif; color: #18222c; padding: 0 1rem; }
.score-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }
.card { border: 1px solid #d8dee5; border-radius: .75rem; padding: 1.25rem; }
.card h2 { font-size: .9rem; text-transform: uppercase; letter-spacing: .08em; margin: 0 0 .5rem; color: #5b6875; }
.points { font-size: 2.5rem; font-weight: 700; color: #0b6e4f; }
.muted { color: #5b6875; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
th, td { padding: .7rem; border-bottom: 1px solid #e5e9ed; text-align: left; }
input, button { padding: .5rem; font: inherit; } button { background: #0b6e4f; color: white; border: 0; border-radius: .3rem; cursor: pointer; }
#result { min-height: 1.3rem; margin-top: .75rem; }
@media (max-width: 600px) { .score-grid { grid-template-columns: 1fr; } table { font-size: .85rem; } }
</style>
<h1>Territory Control</h1>
<p class="muted">Attack Points are spent by physical node attacks. The CTFd scoreboard shows Final Score generated by captured territories.</p>
<div class="score-grid">
  <section class="card"><h2>Attack Points</h2><div class="points">{{ attack_points }}</div><p class="muted">Available for physical territory attacks.</p></section>
  <section class="card"><h2>Team QR Code and Color</h2><img src="/api/v1/territory-control/me/qr" width="150" height="150" alt="Team QR code"><p><label>Node color <input id="team-color" type="color" value="#{{ team_color }}"></label> <button id="save-color">Save color</button></p><p class="muted">Show this QR code to a territory scanner.</p></section>
</div>
<h2>Territories</h2>
<table><thead><tr><th>Territory</th><th>Node</th><th>Owner</th><th>Captured</th></tr></thead><tbody>
{% for territory in territories %}<tr><td>{{ territory.name }}</td><td>{{ territory.node_id }}<br><small>{% if territory.last_seen_at and (now - territory.last_seen_at).total_seconds() <= availability_seconds %}Available{% else %}Unavailable{% endif %}</small></td><td>{% if territory.owner_team_id %}<span style="display:inline-block;width:1em;height:1em;background:#{{ owner_colors[territory.owner_team_id] }};border:1px solid #333"></span> {{ owners.get(territory.owner_team_id, 'Deleted team') }}{% else %}Neutral{% endif %}</td><td>{% if territory.captured_at %}{{ (now - territory.captured_at).total_seconds()|int }} seconds{% else %}-{% endif %}</td></tr>{% endfor %}
</tbody></table><p id="result"></p>
<script>
document.querySelector('#save-color').addEventListener('click', async event => {
  const result = document.querySelector('#result');
  const csrfToken = window.CTFd?.config?.csrfNonce || window.init?.csrfNonce;
  try {
    const response = await fetch('/api/v1/territory-control/me/color', { method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'CSRF-Token': csrfToken}, body: JSON.stringify({color: document.querySelector('#team-color').value}) });
    const data = await response.json();
    result.textContent = response.ok ? 'Team color saved.' : (data.error || `Color could not be saved (HTTP ${response.status}).`);
  } catch (error) {
    result.textContent = 'Color could not be saved: ' + error.message;
  }
});
</script>"""
