from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import os

import qrcode
from flask import abort, jsonify, request, render_template_string, send_file
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from CTFd.models import Challenges, Solves, Teams, db
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge
from CTFd.utils.user import get_current_team
from CTFd.utils.decorators import admins_only

from .models import CaptureSession, DeviceCommand, TeamIdentity, Territory, TerritoryAttack, TerritoryChallenge


BLACK = "000000"
CAPTURE_WINDOW_SECONDS = 30


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
    return bool(secret and request.headers.get("X-Territory-Secret") == secret)


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


def upgrade_schema():
    """Small additive migration for deployments created before capture timing existed."""
    columns = {column["name"] for column in inspect(db.engine).get_columns("territory_control_territories")}
    if "captured_at" not in columns:
        db.session.execute(text("ALTER TABLE territory_control_territories ADD COLUMN captured_at DATETIME"))
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
        owners = {team.id: team.name for team in Teams.query.all()}
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
        )

    @app.post("/api/v1/territory-control/me/color")
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
    def device_attack():
        """Resolve one physical UUID_REQUEST from a trusted serial worker."""
        if not device_authorized():
            abort(403)
        data = request.get_json(silent=True) or {}
        try:
            attack_points = points(data.get("attack_points"), "attack_points")
        except ValueError as error:
            return jsonify(action="reject", error=str(error)), 400
        if attack_points <= 0:
            return jsonify(action="reject", error="attack_points must be greater than zero"), 400
        territory = Territory.query.filter_by(node_id=str(data.get("node_id", "")).lower()).with_for_update().first()
        if territory is None:
            return jsonify(action="reject", error="unknown territory"), 404
        scanned_uuid = canonical_uuid(data.get("uuid"))
        scanned = TeamIdentity.query.filter_by(uuid=scanned_uuid).with_for_update().first() if scanned_uuid else None
        if scanned is None:
            return jsonify(action="reject", error="unknown team UUID"), 403
        identity = identity_for(scanned.team_id, lock=True)
        if identity.attack_points < attack_points:
            return jsonify(action="reject", error="insufficient attack points"), 409
        identity.attack_points -= attack_points
        defense_multiplier = configured_points("TERRITORY_DEFENSE_MULTIPLIER", "1")
        attack_multiplier = configured_points("TERRITORY_ATTACK_MULTIPLIER", "2")
        prior_defense = territory.defense_points
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
        return jsonify(action="color", color=response_color, result=result, defense_points=str(territory.defense_points))

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
                return render_template_string(ADMIN_TEMPLATE, territories=Territory.query.all(), teams=teams, team_names={team.id: team.name for team in teams}, attacks=[], error=str(error)), 400
        territories = Territory.query.order_by(Territory.name).all()
        attacks = TerritoryAttack.query.order_by(TerritoryAttack.created_at.desc()).limit(100).all()
        teams = Teams.query.order_by(Teams.name).all()
        return render_template_string(ADMIN_TEMPLATE, territories=territories, teams=teams, team_names={team.id: team.name for team in teams}, attacks=attacks, error=None)

    @app.post("/admin/territory-control/territories/<int:territory_id>")
    @admins_only
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


ADMIN_TEMPLATE = """<!doctype html><title>Territory Control</title>
<h1>Territory Control</h1>{% if error %}<p style='color:#b00'>{{ error }}</p>{% endif %}
<form method='post'><label>Name <input name='name' required></label> <label>Node ID <input name='node_id' required></label> <label>Defense <input name='defense_points' type='number' min='0' step='0.0001' value='0' required></label> <label>Score amount <input name='score_amount' type='number' min='0' step='1' value='0' required></label> <label>Interval seconds <input name='score_interval_seconds' type='number' min='1' value='300' required></label> <button>Create territory</button></form>
<h2>Territories</h2><table><tr><th>Name / node</th><th>Defense / score</th><th>Owner</th><th>Settings</th></tr>{% for territory in territories %}<tr><td>{{ territory.name }}<br><small>{{ territory.node_id }}</small></td><td>{{ territory.defense_points }} defense<br>{{ territory.score_amount }} / {{ territory.score_interval_seconds }}s</td><td>{% if territory.owner_team_id %}{{ team_names.get(territory.owner_team_id, 'Deleted team') }}{% else %}Neutral{% endif %}</td><td><form method='post' action='/admin/territory-control/territories/{{ territory.id }}'><input name='name' value='{{ territory.name }}' required><input name='node_id' value='{{ territory.node_id }}' required><input name='defense_points' value='{{ territory.defense_points }}' type='number' min='0' step='0.0001' required><input name='score_amount' value='{{ territory.score_amount }}' type='number' min='0' step='1' required><input name='score_interval_seconds' value='{{ territory.score_interval_seconds }}' type='number' min='1' required><button>Save</button></form><form method='post' action='/admin/territory-control/territories/{{ territory.id }}/owner'><select name='owner_team_id'><option value=''>Neutral</option>{% for team in teams %}<option value='{{ team.id }}' {% if team.id == territory.owner_team_id %}selected{% endif %}>{{ team.name }}</option>{% endfor %}</select><button>Set owner</button></form></td></tr>{% endfor %}</table>
<h2>Team Attack Points</h2><table><tr><th>Team</th><th>Adjustment</th></tr>{% for team in teams %}<tr><td>{{ team.name }}</td><td><form method='post' action='/admin/territory-control/teams/{{ team.id }}/attack-points'><input name='delta' type='number' step='0.0001' placeholder='+/- AP' required><button>Apply</button></form></td></tr>{% endfor %}</table>
<h2>Recent Territory Events</h2><table><tr><th>Time</th><th>Territory</th><th>Team</th><th>AP</th><th>Result</th></tr>{% for attack in attacks %}<tr><td>{{ attack.created_at }}</td><td>#{{ attack.territory_id }}</td><td>{{ team_names.get(attack.team_id, 'Admin') }}</td><td>{{ attack.attack_points }}</td><td>{{ attack.result }}{% if attack.note %}: {{ attack.note }}{% endif %}</td></tr>{% endfor %}</table>"""


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
<table><thead><tr><th>Territory</th><th>Node</th><th>Owner</th><th>Captured</th><th>Defense</th><th>CTFd yield</th></tr></thead><tbody>
{% for territory in territories %}<tr><td>{{ territory.name }}</td><td>{{ territory.node_id }}</td><td>{% if territory.owner_team_id %}<span style="display:inline-block;width:1em;height:1em;background:#{{ owner_colors[territory.owner_team_id] }};border:1px solid #333"></span> {{ owners.get(territory.owner_team_id, 'Deleted team') }}{% else %}Neutral{% endif %}</td><td>{% if territory.captured_at %}{{ (now - territory.captured_at).total_seconds()|int }} seconds{% else %}-{% endif %}</td><td>{{ territory.defense_points }}</td><td>+{{ territory.score_amount }} / {{ territory.score_interval_seconds }}s</td></tr>{% endfor %}
</tbody></table><p id="result"></p>
<script>
document.querySelector('#save-color').addEventListener('click', async event => {
  const result = document.querySelector('#result');
  const response = await fetch('/api/v1/territory-control/me/color', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({color: document.querySelector('#team-color').value}) });
  const data = await response.json();
  result.textContent = response.ok ? 'Team color saved.' : (data.error || 'Color could not be saved.');
});
</script>"""
